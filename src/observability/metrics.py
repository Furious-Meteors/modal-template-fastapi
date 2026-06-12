"""
src/observability/metrics.py
=============================
THIS FILE IS A STUB. Replace it entirely in each new service.
The template ships this empty on purpose.

─────────────────────────────────────────────────────────────
CONTRACT
─────────────────────────────────────────────────────────────
• Import get_meter from .setup — never instantiate MeterProvider directly.
• Name your instruments with a service prefix:  "myservice.thing.unit"
• Use snake_case for Python variables, dot.notation for metric names.
• All instruments are module-level singletons — create once, use everywhere.
• Labels (attributes) are added at record time, not at instrument creation.

─────────────────────────────────────────────────────────────
EXAMPLE  (delete this and replace with your own)
─────────────────────────────────────────────────────────────

from src.observability.setup import get_meter

_meter = get_meter("modal-fastapi")

# HTTP layer — these are already recorded by TelemetryMiddleware.
# Only add business-level instruments here.

items_created = _meter.create_counter(
    name="app.items.created",
    description="Items successfully created via POST /items",
    unit="1",
)

items_fetch_duration = _meter.create_histogram(
    name="app.items.fetch.duration",
    description="Time to fetch a single item from the store, in ms",
    unit="ms",
)

# Usage in your route handler:
#   from src.observability.metrics import items_created, items_fetch_duration
#   items_created.add(1, {"item_type": item.type})
#   items_fetch_duration.record(elapsed_ms, {"cache_hit": "false"})
"""

# Leave this file empty until you have real business metrics to add.
# The middleware handles all HTTP-level metrics automatically.