"""swagger.yaml is hand-curated; these tests stop it drifting from the real API."""
import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")

from src.main import app

PREFIX = "/api/v1"
SPEC = pathlib.Path(__file__).resolve().parent.parent / "swagger.yaml"
# OpenAPI cannot express a WebSocket; swagger.yaml documents it as a GET with a 101.
DOCUMENTED_ONLY = {("/ws", "get")}
GENERATED_ONLY_SCHEMAS = {"HTTPValidationError", "ValidationError", "HealthStatus"}  # swagger uses ErrorDetail / an enum


@pytest.fixture(scope="module")
def doc():
    return yaml.safe_load(SPEC.read_text())


@pytest.fixture(scope="module")
def generated():
    return app.openapi()


def _operations(spec, strip=""):
    ops = {}
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method in {"get", "post", "put", "delete", "patch"}:
                ops[(path.removeprefix(strip), method)] = op
    return ops


def test_every_endpoint_is_documented_and_nothing_extra(doc, generated):
    real = set(_operations(generated, PREFIX))
    documented = set(_operations(doc)) - DOCUMENTED_ONLY
    assert documented == real


def test_response_status_codes_match(doc, generated):
    real = _operations(generated, PREFIX)
    for key, op in _operations(doc).items():
        if key in DOCUMENTED_ONLY:
            continue
        # swagger may add what FastAPI does not declare (401/403/503 from dependencies and handlers)
        assert set(real[key]["responses"]) <= set(op["responses"]), key


def test_schemas_match_property_and_required_lists(doc, generated):
    real = generated["components"]["schemas"]
    ours = doc["components"]["schemas"]
    names = set(real) - GENERATED_ONLY_SCHEMAS
    assert names <= set(ours), f"undocumented schemas: {names - set(ours)}"
    for name in names:
        assert set(ours[name]["properties"]) == set(real[name]["properties"]), name
        assert set(ours[name].get("required", [])) == set(real[name].get("required", [])), name


def test_query_parameters_match(doc, generated):
    real = _operations(generated, PREFIX)
    for key, op in _operations(doc).items():
        if key in DOCUMENTED_ONLY:
            continue
        ours = {p["name"] for p in op.get("parameters", []) if p["in"] == "query"}
        theirs = {p["name"] for p in real[key].get("parameters", []) if p["in"] == "query"}
        assert ours == theirs, key


def test_every_documented_error_code_exists_in_the_source(doc):
    table = doc["info"]["description"]
    codes = set(re.findall(r"`([A-Z][A-Z_]{4,})`", table)) - {"RATE_LIMITED_X"}
    source = "".join(p.read_text() for p in pathlib.Path("src").rglob("*.py"))
    missing = {c for c in codes if f'"{c}"' not in source}
    assert not missing, f"documented but not in src: {missing}"


def test_spec_is_valid_openapi():
    validator = pytest.importorskip("openapi_spec_validator")
    validator.validate(yaml.safe_load(SPEC.read_text()))
