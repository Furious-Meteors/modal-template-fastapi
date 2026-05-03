"""Unit tests for Pydantic models in src/api/models.py."""
import pytest
from pydantic import ValidationError

from src.api.models import (
    BaseResponse,
    ErrorDetail,
    FileUploadRequest,
    FileUploadResponse,
    GenericRequest,
    HealthCheckResponse,
    HealthStatus,
    ItemResponse,
    TokenPayload,
)


# ---------------------------------------------------------------------------
# GenericRequest
# ---------------------------------------------------------------------------


def test_generic_request_requires_data():
    with pytest.raises(ValidationError):
        GenericRequest()  # type: ignore[call-arg]


def test_generic_request_valid():
    req = GenericRequest(data={"key": "value"})
    assert req.data == {"key": "value"}
    assert req.project_id is None


def test_generic_request_with_project_id():
    req = GenericRequest(data={}, project_id="proj-123")
    assert req.project_id == "proj-123"


def test_generic_request_empty_data_allowed():
    req = GenericRequest(data={})
    assert req.data == {}


def test_generic_request_nested_data():
    req = GenericRequest(data={"nested": {"a": 1}})
    assert req.data["nested"]["a"] == 1


def test_generic_request_project_id_min_length():
    with pytest.raises(ValidationError):
        GenericRequest(data={}, project_id="")


# ---------------------------------------------------------------------------
# BaseResponse
# ---------------------------------------------------------------------------


def test_base_response_requires_session_id():
    with pytest.raises(ValidationError):
        BaseResponse()  # type: ignore[call-arg]


def test_base_response_valid():
    resp = BaseResponse(session_id="abc-123")
    assert resp.session_id == "abc-123"


# ---------------------------------------------------------------------------
# ItemResponse
# ---------------------------------------------------------------------------


def test_item_response_valid():
    resp = ItemResponse(session_id="s1", status="ok", message="done")
    assert resp.data is None


def test_item_response_with_data():
    resp = ItemResponse(session_id="s1", status="ok", message="done", data={"id": "x"})
    assert resp.data["id"] == "x"


def test_item_response_requires_status():
    with pytest.raises(ValidationError):
        ItemResponse(session_id="s1", message="done")  # type: ignore[call-arg]


def test_item_response_requires_message():
    with pytest.raises(ValidationError):
        ItemResponse(session_id="s1", status="ok")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# HealthCheckResponse
# ---------------------------------------------------------------------------


def test_health_check_response_valid():
    resp = HealthCheckResponse(
        session_id="s1",
        status=HealthStatus.HEALTHY,
        service_name="svc",
        version="1.0.0",
        services_summary={"total": 1, "healthy": 1, "unhealthy": 0},
    )
    assert resp.status == HealthStatus.HEALTHY


def test_health_status_enum_values():
    assert HealthStatus.HEALTHY == "healthy"
    assert HealthStatus.UNHEALTHY == "unhealthy"


def test_health_check_response_unhealthy():
    resp = HealthCheckResponse(
        session_id="s1",
        status=HealthStatus.UNHEALTHY,
        service_name="svc",
        version="1.0.0",
        services_summary={"total": 1, "healthy": 0, "unhealthy": 1},
    )
    assert resp.status == HealthStatus.UNHEALTHY


def test_health_check_response_requires_summary():
    with pytest.raises(ValidationError):
        HealthCheckResponse(
            session_id="s1",
            status=HealthStatus.HEALTHY,
            service_name="svc",
            version="1.0.0",
        )


# ---------------------------------------------------------------------------
# ErrorDetail
# ---------------------------------------------------------------------------


def test_error_detail_only_detail():
    err = ErrorDetail(detail="something went wrong")
    assert err.session_id is None
    assert err.error_code is None


def test_error_detail_full():
    err = ErrorDetail(detail="oops", session_id="sid", error_code="ERR_001")
    assert err.error_code == "ERR_001"
    assert err.session_id == "sid"


def test_error_detail_requires_detail():
    with pytest.raises(ValidationError):
        ErrorDetail()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# TokenPayload
# ---------------------------------------------------------------------------


def test_token_payload_valid():
    payload = TokenPayload(sub="user@example.com", exp=9_999_999_999)
    assert payload.iat is None


def test_token_payload_with_iat():
    payload = TokenPayload(sub="u", exp=9_999_999_999, iat=1_000_000_000)
    assert payload.iat == 1_000_000_000


def test_token_payload_requires_sub():
    with pytest.raises(ValidationError):
        TokenPayload(exp=9_999_999_999)  # type: ignore[call-arg]


def test_token_payload_requires_exp():
    with pytest.raises(ValidationError):
        TokenPayload(sub="u")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# FileUploadRequest
# ---------------------------------------------------------------------------


def test_file_upload_request_valid():
    req = FileUploadRequest(project_id="proj-1")
    assert req.filename is None


def test_file_upload_request_with_filename():
    req = FileUploadRequest(project_id="proj-1", filename="report.pdf")
    assert req.filename == "report.pdf"


def test_file_upload_request_project_id_required():
    with pytest.raises(ValidationError):
        FileUploadRequest()  # type: ignore[call-arg]


def test_file_upload_request_project_id_min_length():
    with pytest.raises(ValidationError):
        FileUploadRequest(project_id="")


# ---------------------------------------------------------------------------
# FileUploadResponse
# ---------------------------------------------------------------------------


def test_file_upload_response_minimal():
    resp = FileUploadResponse(session_id="s1", status="ok", message="uploaded")
    assert resp.file_id is None
    assert resp.file_size is None
    assert resp.file_path is None


def test_file_upload_response_full():
    resp = FileUploadResponse(
        session_id="s1",
        status="ok",
        message="done",
        file_id="f-abc",
        file_size=1024,
        file_path="/tmp/file.pdf",
    )
    assert resp.file_id == "f-abc"
    assert resp.file_size == 1024
