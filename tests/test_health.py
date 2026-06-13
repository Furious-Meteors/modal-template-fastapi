from fastapi.testclient import TestClient
import os


def test_health_returns_200(client: TestClient):
    response = client.get("/api/v1/health")
    assert response.status_code == 200


def test_health_status_is_healthy(client: TestClient):
    data = client.get("/api/v1/health").json()
    assert data["status"] == "healthy"


def test_health_service_name(client: TestClient):
    data = client.get("/api/v1/health").json()
    expected_service_name = f"modal-template-fastapi-{os.environ.get('MODAL_ENV', 'dev')}"
    assert data["service_name"] == expected_service_name


def test_health_version(client: TestClient):
    from src.utils.config import APP_VERSION
    data = client.get("/api/v1/health").json()
    assert data["version"] == APP_VERSION


def test_health_has_session_id(client: TestClient):
    data = client.get("/api/v1/health").json()
    assert "session_id" in data
    assert data["session_id"]  # non-empty


def test_health_services_summary_shape(client: TestClient):
    summary = client.get("/api/v1/health").json()["services_summary"]
    assert "total" in summary
    assert "healthy" in summary
    assert "unhealthy" in summary


def test_health_session_id_unique_per_request(client: TestClient):
    id1 = client.get("/api/v1/health").json()["session_id"]
    id2 = client.get("/api/v1/health").json()["session_id"]
    assert id1 != id2


def test_health_does_not_require_auth(auth_client: TestClient):
    """Health endpoint is public — no Authorization header needed."""
    response = auth_client.get("/api/v1/health")
    assert response.status_code == 200
