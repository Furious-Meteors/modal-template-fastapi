import uuid
from typing import Any, Dict, List, Optional

from src.api.models import GenericRequest

_store: Dict[str, Dict[str, Any]] = {}


def create_item(request: GenericRequest) -> Dict[str, Any]:
    item_id = str(uuid.uuid4())
    _store[item_id] = {
        "id": item_id,
        "project_id": request.project_id,
        **request.data,
    }
    return _store[item_id]


def get_item(item_id: str) -> Optional[Dict[str, Any]]:
    return _store.get(item_id)


def list_items() -> List[Dict[str, Any]]:
    return list(_store.values())


def update_item(item_id: str, request: GenericRequest) -> Optional[Dict[str, Any]]:
    if item_id not in _store:
        return None
    _store[item_id] = {
        "id": item_id,
        "project_id": request.project_id,
        **request.data,
    }
    return _store[item_id]


def delete_item(item_id: str) -> bool:
    if item_id not in _store:
        return False
    del _store[item_id]
    return True
