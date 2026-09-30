"""
Integration tests for GET /api/objects

Beyond checking the HTTP response, these tests act as a contract between the
objects catalog (models/objects.py) and the extraction service that consumes it.
"""

import hashlib
import hmac
import time
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from auth.hmac_auth import verify_hmac
from models.objects import SUPPORTED_OBJECTS, SupportedObject
from routers import objects
import services.extraction_service as extraction_service

CORE_KEY = "core-key-" + "x" * 32
ENGINEER_KEY = "engineer-key-" + "y" * 32
EXPECTED_FIELDS = {"name", "label", "soql_template", "default_fields", "supports_incremental"}


def sign(body="", key=CORE_KEY, key_id="core", timestamp=None):
    ts = str(time.time() if timestamp is None else timestamp)
    sig = hmac.new(key.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return {"X-Signature": sig, "X-Timestamp": ts, "X-Key-Id": key_id}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(objects.router)
    app.dependency_overrides[verify_hmac] = lambda: None
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def supported(client):
    resp = client.get("/api/objects")
    assert resp.status_code == 200
    return resp.json()["supported_objects"]


@pytest.fixture
def secured_client():
    app = FastAPI()

    @app.middleware("http")
    async def capture_raw_body(request: Request, call_next):
        request.state.raw_body = await request.body()
        return await call_next(request)

    app.include_router(objects.router)
    app.state.settings = MagicMock(
        HMAC_ENABLED=True,
        HMAC_SECRET_KEY_CORE=CORE_KEY,
        HMAC_SECRET_KEY_ENGINEER=ENGINEER_KEY,
        HMAC_SIGNATURE_MAX_AGE=300,
    )
    return TestClient(app)


# ===========================================================================
# Response shape
# ===========================================================================
def test_objects_returns_wrapped_list(client):
    resp = client.get("/api/objects")
    body = resp.json()

    assert resp.status_code == 200
    assert list(body) == ["supported_objects"]
    assert isinstance(body["supported_objects"], list)
    assert len(body["supported_objects"]) > 0


def test_objects_includes_core_first_release_objects(supported):
    names = {o["name"] for o in supported}
    assert {"Contact", "Account", "Opportunity"} <= names


def test_each_object_has_exactly_the_documented_fields(supported):
    for obj in supported:
        assert set(obj) == EXPECTED_FIELDS, obj["name"]


def test_field_types(supported):
    for obj in supported:
        assert isinstance(obj["name"], str) and obj["name"]
        assert isinstance(obj["label"], str) and obj["label"]
        assert isinstance(obj["soql_template"], str)
        assert isinstance(obj["default_fields"], list) and obj["default_fields"]
        assert all(isinstance(f, str) for f in obj["default_fields"])
        assert isinstance(obj["supports_incremental"], bool)


def test_response_matches_catalog(supported):
    assert supported == [o.model_dump() for o in SUPPORTED_OBJECTS]


def test_object_names_are_unique(supported):
    names = [o["name"] for o in supported]
    assert len(names) == len(set(names))


def test_objects_only_allows_get(client):
    assert client.post("/api/objects").status_code == 405


def test_route_reads_the_live_catalog(client, monkeypatch):
    custom = [
        SupportedObject(
            name="Lead", label="Leads",
            soql_template="SELECT {fields} FROM Lead {where_clause}",
            default_fields=["Id"], supports_incremental=False,
        )
    ]
    monkeypatch.setattr(objects, "SUPPORTED_OBJECTS", custom)

    body = client.get("/api/objects").json()

    assert [o["name"] for o in body["supported_objects"]] == ["Lead"]


# ===========================================================================
# Catalog contract with the extraction service
# ===========================================================================
def test_templates_contain_required_placeholders(supported):
    for obj in supported:
        assert "{fields}" in obj["soql_template"], obj["name"]
        assert "{where_clause}" in obj["soql_template"], obj["name"]


def test_templates_render_to_soql_for_the_right_object(supported):
    for obj in supported:
        rendered = obj["soql_template"].format(fields="Id", where_clause="")
        assert rendered.startswith("SELECT Id FROM "), obj["name"]
        assert f"FROM {obj['name']}" in rendered, obj["name"]


def test_default_fields_are_clean_soql_identifiers(supported):
    """Fields are joined with ', ' and interpolated into SOQL, so keep them clean."""
    for obj in supported:
        fields = obj["default_fields"]
        assert len(fields) == len(set(fields)), f"duplicate fields in {obj['name']}"
        for f in fields:
            assert f.replace("_", "").replace(".", "").isalnum(), f"{obj['name']}: {f!r}"


def test_every_object_selects_id(supported):
    """Deduplication keys on Id, so every object must fetch it."""
    for obj in supported:
        assert "Id" in obj["default_fields"], obj["name"]


def test_incremental_objects_expose_last_modified_date(supported):
    """The incremental filter is `LastModifiedDate >= ...`."""
    for obj in supported:
        if obj["supports_incremental"]:
            assert "LastModifiedDate" in obj["default_fields"], obj["name"]
            assert "LastModifiedDate" in obj["soql_template"], obj["name"]


def test_extraction_service_can_resolve_every_advertised_object(supported):
    names = [o["name"] for o in supported]
    assert extraction_service.resolve_objects(names) == names


def test_extraction_service_can_build_soql_for_every_advertised_object(supported):
    for obj in supported:
        soql = extraction_service.build_soql(obj["name"], None)
        assert soql.startswith("SELECT ") and f"FROM {obj['name']}" in soql, obj["name"]

        incremental = extraction_service.build_soql(obj["name"], "2026-04-01T00:00:00Z")
        if obj["supports_incremental"]:
            assert "LastModifiedDate >= 2026-04-01T00:00:00Z" in incremental, obj["name"]


@pytest.mark.xfail(
    reason="Design doc section 3.3 plans Lead, User, CampaignMember and Activity "
           "(Task + Event), but models/objects.py only lists the first three.",
    strict=False,
)
@pytest.mark.parametrize("name", ["Lead", "User", "CampaignMember", "Task", "Event"])
def test_planned_objects_are_advertised(supported, name):
    assert name in {o["name"] for o in supported}


# ===========================================================================
# HMAC enforcement (real verify_hmac)
# ===========================================================================
def test_objects_requires_signature_headers(secured_client):
    assert secured_client.get("/api/objects").status_code == 422


def test_objects_rejects_bad_signature(secured_client):
    assert secured_client.get("/api/objects", headers=sign(key="wrong")).status_code == 401


def test_objects_rejects_expired_signature(secured_client):
    resp = secured_client.get("/api/objects", headers=sign(timestamp=time.time() - 3600))
    assert resp.status_code == 401


def test_objects_accepts_core_key(secured_client):
    resp = secured_client.get("/api/objects", headers=sign())

    assert resp.status_code == 200
    assert "supported_objects" in resp.json()


def test_objects_accepts_engineer_key(secured_client):
    resp = secured_client.get("/api/objects", headers=sign(key=ENGINEER_KEY, key_id="engineer"))
    assert resp.status_code == 200