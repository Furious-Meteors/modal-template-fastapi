"""Tests for CRUD item endpoints (auth dependency overridden via client fixture)."""
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_item(client: TestClient, data: dict = None, project_id: str = None) -> dict:
    payload: dict = {"data": data or {"name": "widget", "value": 1}}
    if project_id:
        payload["project_id"] = project_id
    return client.post("/api/v1/items", json=payload).json()


# ---------------------------------------------------------------------------
# List items
# ---------------------------------------------------------------------------


def test_list_items_empty(client: TestClient):
    response = client.get("/api/v1/items")
    assert response.status_code == 200
    assert response.json() == []


def test_list_items_returns_all_created(client: TestClient):
    _create_item(client, {"name": "alpha"})
    _create_item(client, {"name": "beta"})
    items = client.get("/api/v1/items").json()
    assert len(items) == 2


def test_list_items_contains_correct_data(client: TestClient):
    _create_item(client, {"name": "gamma"})
    items = client.get("/api/v1/items").json()
    names = [i["name"] for i in items]
    assert "gamma" in names


# ---------------------------------------------------------------------------
# Create item
# ---------------------------------------------------------------------------


def test_create_item_returns_201(client: TestClient):
    response = client.post("/api/v1/items", json={"data": {"name": "new"}})
    assert response.status_code == 201


def test_create_item_response_status(client: TestClient):
    data = _create_item(client)
    assert data["status"] == "created"


def test_create_item_response_message(client: TestClient):
    data = _create_item(client)
    assert data["message"] == "Item created successfully"


def test_create_item_has_session_id(client: TestClient):
    data = _create_item(client)
    assert "session_id" in data and data["session_id"]


def test_create_item_data_has_id(client: TestClient):
    data = _create_item(client)
    assert "id" in data["data"]


def test_create_item_persists_payload(client: TestClient):
    data = _create_item(client, {"name": "persisted", "count": 7})
    assert data["data"]["name"] == "persisted"
    assert data["data"]["count"] == 7


def test_create_item_persists_project_id(client: TestClient):
    data = _create_item(client, {"name": "x"}, project_id="proj-42")
    assert data["data"]["project_id"] == "proj-42"


def test_create_item_project_id_optional(client: TestClient):
    response = client.post("/api/v1/items", json={"data": {"k": "v"}})
    assert response.status_code == 201


def test_create_item_missing_data_returns_422(client: TestClient):
    response = client.post("/api/v1/items", json={})
    assert response.status_code == 422


def test_create_item_ids_are_unique(client: TestClient):
    id1 = _create_item(client)["data"]["id"]
    id2 = _create_item(client)["data"]["id"]
    assert id1 != id2


# ---------------------------------------------------------------------------
# Get item
# ---------------------------------------------------------------------------


def test_get_item_returns_200(client: TestClient):
    item_id = _create_item(client)["data"]["id"]
    response = client.get(f"/api/v1/items/{item_id}")
    assert response.status_code == 200


def test_get_item_correct_id(client: TestClient):
    item_id = _create_item(client)["data"]["id"]
    data = client.get(f"/api/v1/items/{item_id}").json()
    assert data["data"]["id"] == item_id


def test_get_item_response_status(client: TestClient):
    item_id = _create_item(client)["data"]["id"]
    data = client.get(f"/api/v1/items/{item_id}").json()
    assert data["status"] == "success"


def test_get_item_not_found_returns_404(client: TestClient):
    response = client.get("/api/v1/items/does-not-exist")
    assert response.status_code == 404


def test_get_item_not_found_detail(client: TestClient):
    response = client.get("/api/v1/items/ghost-id")
    assert "ghost-id" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Update item
# ---------------------------------------------------------------------------


def test_update_item_returns_200(client: TestClient):
    item_id = _create_item(client)["data"]["id"]
    response = client.put(f"/api/v1/items/{item_id}", json={"data": {"name": "updated"}})
    assert response.status_code == 200


def test_update_item_response_status(client: TestClient):
    item_id = _create_item(client)["data"]["id"]
    data = client.put(f"/api/v1/items/{item_id}", json={"data": {"x": 1}}).json()
    assert data["status"] == "updated"


def test_update_item_reflects_new_data(client: TestClient):
    item_id = _create_item(client, {"name": "before"})["data"]["id"]
    data = client.put(
        f"/api/v1/items/{item_id}", json={"data": {"name": "after"}}
    ).json()
    assert data["data"]["name"] == "after"


def test_update_item_preserves_id(client: TestClient):
    item_id = _create_item(client)["data"]["id"]
    data = client.put(
        f"/api/v1/items/{item_id}", json={"data": {"name": "new"}}
    ).json()
    assert data["data"]["id"] == item_id


def test_update_item_not_found_returns_404(client: TestClient):
    response = client.put("/api/v1/items/ghost-id", json={"data": {"x": 1}})
    assert response.status_code == 404


def test_update_item_missing_data_returns_422(client: TestClient):
    item_id = _create_item(client)["data"]["id"]
    response = client.put(f"/api/v1/items/{item_id}", json={})
    assert response.status_code == 422


def test_update_item_visible_on_get(client: TestClient):
    item_id = _create_item(client, {"name": "old"})["data"]["id"]
    client.put(f"/api/v1/items/{item_id}", json={"data": {"name": "new"}})
    fetched = client.get(f"/api/v1/items/{item_id}").json()["data"]
    assert fetched["name"] == "new"


# ---------------------------------------------------------------------------
# Delete item
# ---------------------------------------------------------------------------


def test_delete_item_returns_204(client: TestClient):
    item_id = _create_item(client)["data"]["id"]
    response = client.delete(f"/api/v1/items/{item_id}")
    assert response.status_code == 204


def test_delete_item_removes_from_store(client: TestClient):
    item_id = _create_item(client)["data"]["id"]
    client.delete(f"/api/v1/items/{item_id}")
    assert client.get(f"/api/v1/items/{item_id}").status_code == 404


def test_delete_item_removed_from_list(client: TestClient):
    item_id = _create_item(client)["data"]["id"]
    client.delete(f"/api/v1/items/{item_id}")
    items = client.get("/api/v1/items").json()
    ids = [i["id"] for i in items]
    assert item_id not in ids


def test_delete_item_not_found_returns_404(client: TestClient):
    response = client.delete("/api/v1/items/ghost-id")
    assert response.status_code == 404


def test_delete_only_target_item(client: TestClient):
    id1 = _create_item(client, {"name": "keep"})["data"]["id"]
    id2 = _create_item(client, {"name": "remove"})["data"]["id"]
    client.delete(f"/api/v1/items/{id2}")
    assert client.get(f"/api/v1/items/{id1}").status_code == 200
