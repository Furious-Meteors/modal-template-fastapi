"""
Drive the ASGI app directly so tests can observe a response *while it is still
open*. Starlette's TestClient buffers the whole body, which makes it unusable
for open-ended streams such as SSE.
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional
from urllib.parse import urlsplit


async def wait_until(predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.005) -> None:
    """
    Poll until `predicate()` is true. Use instead of a fixed sleep: it returns as
    soon as the condition holds and tolerates a slow CI runner up to `timeout`.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)


class OpenStream:
    def __init__(self, app, path: str, headers: Optional[dict] = None, method: str = "GET"):
        split = urlsplit(path)
        self._sent: asyncio.Queue = asyncio.Queue()
        self._received: asyncio.Queue = asyncio.Queue()
        self._received.put_nowait({"type": "http.request", "body": b"", "more_body": False})
        self.start: Optional[dict] = None
        self._buffer = b""
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": split.path,
            "raw_path": split.path.encode(),
            "query_string": split.query.encode(),
            "root_path": "",
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        self.task = asyncio.ensure_future(app(scope, self._receive, self._send))

    async def _receive(self):
        return await self._received.get()

    async def _send(self, message):
        await self._sent.put(message)

    async def wait_started(self, timeout: float = 2.0) -> dict:
        while self.start is None:
            message = await asyncio.wait_for(self._sent.get(), timeout)
            if message["type"] == "http.response.start":
                self.start = message
        return self.start

    @property
    def status(self) -> int:
        return self.start["status"]

    @property
    def headers(self) -> dict[str, str]:
        return {k.decode().lower(): v.decode() for k, v in self.start["headers"]}

    async def read_until(self, needle: bytes, timeout: float = 2.0) -> bytes:
        """Read body chunks until `needle` appears in the accumulated body."""
        await self.wait_started(timeout)
        while needle not in self._buffer:
            message = await asyncio.wait_for(self._sent.get(), timeout)
            if message["type"] == "http.response.body":
                self._buffer += message.get("body", b"")
                if not message.get("more_body", False):
                    break
        return self._buffer

    async def body_until_end(self, timeout: float = 2.0) -> bytes:
        await self.wait_started(timeout)
        while True:
            message = await asyncio.wait_for(self._sent.get(), timeout)
            if message["type"] == "http.response.body":
                self._buffer += message.get("body", b"")
                if not message.get("more_body", False):
                    return self._buffer

    async def disconnect(self, timeout: float = 2.0) -> None:
        self._received.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(self.task, timeout)
