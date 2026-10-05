"""
Integration tests for GET /api/key/verify (routers/verify.py) and the
verify_hmac dependency behind it (auth/hmac_auth.py).

verify_hmac runs for real here; only app.state.settings is a stand-in.
"""

import hashlib
import hmac
import time
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from routers import verify

CORE_KEY = "core-key-" + "x" * 32
ENGINEER_KEY = "engineer-key-" + "y" * 32

CORE_PERMISSIONS = [
    "scan:start", "scan:list", "scan:status", "scan:cancel",
    "scan:resume", "scan:remove", "scan:statistics",
]
ENGINEER_PERMISSIONS = ["maintenance:cleanup", *CORE_PERMISSIONS]


def sign(body="", key=CORE_KEY, key_id="core", timestamp=None):
    ts = str(time.time() if timestamp is None else timestamp)
    sig = hmac.new(key.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return {"X-Signature": sig, "X-Timestamp": ts, "X-Key-Id": key_id}


def build_app(hmac_enabled=True):
    app = FastAPI()

    @app.middleware("http")
    async def capture_raw_body(request: Request, call_next):
        request.state.raw_body = await request.body()
        return await call_next(request)

    app.include_router(verify.router)
    app.state.settings = MagicMock(
        HMAC_ENABLED=hmac_enabled,
        HMAC_SECRET_KEY_CORE=CORE_KEY,
        HMAC_SECRET_KEY_ENGINEER=ENGINEER_KEY,
        HMAC_SIGNATURE_MAX_AGE=300,
    )
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def client():
    return TestClient(build_app(hmac_enabled=True))


@pytest.fixture
def open_client():
    """HMAC_ENABLED=False, as in dev."""
    return TestClient(build_app(hmac_enabled=False))


# ===========================================================================
# Valid keys
# ===========================================================================
def test_verify_core_key(client):
    resp = client.get("/api/key/verify", headers=sign())
    body = resp.json()

    assert resp.status_code == 200
    assert body["valid"] is True
    assert body["key_id"] == "core"
    assert body["hmac_enabled"] is True
    assert body["permissions"] == CORE_PERMISSIONS


def test_verify_engineer_key(client):
    resp = client.get("/api/key/verify", headers=sign(key=ENGINEER_KEY, key_id="engineer"))
    body = resp.json()

    assert resp.status_code == 200
    assert body["valid"] is True
    assert body["key_id"] == "engineer"
    assert body["permissions"] == ENGINEER_PERMISSIONS


def test_verify_response_shape(client):
    body = client.get("/api/key/verify", headers=sign()).json()
    assert set(body) == {"valid", "key_id", "hmac_enabled", "permissions"}


def test_only_engineer_key_can_run_maintenance(client):
    core = client.get("/api/key/verify", headers=sign()).json()["permissions"]
    engineer = client.get(
        "/api/key/verify", headers=sign(key=ENGINEER_KEY, key_id="engineer")
    ).json()["permissions"]

    assert "maintenance:cleanup" not in core
    assert "maintenance:cleanup" in engineer


def test_engineer_permissions_are_a_superset_of_core(client):
    core = client.get("/api/key/verify", headers=sign()).json()["permissions"]
    engineer = client.get(
        "/api/key/verify", headers=sign(key=ENGINEER_KEY, key_id="engineer")
    ).json()["permissions"]

    assert set(core) < set(engineer)


def test_key_id_defaults_to_core_when_header_omitted(client):
    headers = sign()
    del headers["X-Key-Id"]

    resp = client.get("/api/key/verify", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["key_id"] == "core"


def test_signature_within_max_age_is_accepted(client):
    resp = client.get("/api/key/verify", headers=sign(timestamp=time.time() - 200))
    assert resp.status_code == 200


def test_verify_only_allows_get(client):
    assert client.post("/api/key/verify", headers=sign()).status_code == 405


# ===========================================================================
# Rejections
# ===========================================================================
def test_missing_signature_header(client):
    headers = sign()
    del headers["X-Signature"]
    assert client.get("/api/key/verify", headers=headers).status_code == 422


def test_missing_timestamp_header(client):
    headers = sign()
    del headers["X-Timestamp"]
    assert client.get("/api/key/verify", headers=headers).status_code == 422


def test_no_headers_at_all(client):
    assert client.get("/api/key/verify").status_code == 422


def test_wrong_secret(client):
    resp = client.get("/api/key/verify", headers=sign(key="not-the-secret"))

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid signature"


def test_garbage_signature(client):
    headers = {**sign(), "X-Signature": "deadbeef"}
    assert client.get("/api/key/verify", headers=headers).status_code == 401


def test_empty_signature(client):
    headers = {**sign(), "X-Signature": ""}
    assert client.get("/api/key/verify", headers=headers).status_code in (401, 422)


def test_unknown_key_id(client):
    resp = client.get("/api/key/verify", headers=sign(key_id="intruder"))

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Unknown key id"


def test_core_secret_cannot_be_used_under_engineer_id(client):
    resp = client.get("/api/key/verify", headers=sign(key=CORE_KEY, key_id="engineer"))
    assert resp.status_code == 401


def test_engineer_secret_cannot_be_used_under_core_id(client):
    resp = client.get("/api/key/verify", headers=sign(key=ENGINEER_KEY, key_id="core"))
    assert resp.status_code == 401


def test_expired_signature(client):
    resp = client.get("/api/key/verify", headers=sign(timestamp=time.time() - 3600))

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Signature has expired"


def test_far_future_timestamp_is_rejected(client):
    resp = client.get("/api/key/verify", headers=sign(timestamp=time.time() + 3600))
    assert resp.status_code == 401


def test_signature_is_bound_to_timestamp(client):
    """Reusing a valid signature with a fresh timestamp must fail."""
    old = sign(timestamp=time.time() - 10)
    replay = {**old, "X-Timestamp": str(time.time())}

    assert client.get("/api/key/verify", headers=replay).status_code == 401


def test_signature_is_bound_to_body(client):
    ok = client.request("GET", "/api/key/verify", content="hello", headers=sign(body="hello"))
    tampered = client.request("GET", "/api/key/verify", content="tampered", headers=sign(body="hello"))

    assert ok.status_code == 200
    assert tampered.status_code == 401


# ===========================================================================
# HMAC disabled (dev mode)
# ===========================================================================
def test_disabled_hmac_skips_verification(open_client):
    headers = {"X-Signature": "anything", "X-Timestamp": "0", "X-Key-Id": "core"}

    resp = open_client.get("/api/key/verify", headers=headers)
    body = resp.json()

    assert resp.status_code == 200
    assert body["valid"] is True
    assert body["hmac_enabled"] is False
    assert body["permissions"] == CORE_PERMISSIONS


def test_disabled_hmac_still_reports_permissions_for_requested_key_id(open_client):
    headers = {"X-Signature": "x", "X-Timestamp": "0", "X-Key-Id": "engineer"}

    body = open_client.get("/api/key/verify", headers=headers).json()

    assert body["key_id"] == "engineer"
    assert "maintenance:cleanup" in body["permissions"]


def test_disabled_hmac_unknown_key_id_gets_no_permissions(open_client):
    headers = {"X-Signature": "x", "X-Timestamp": "0", "X-Key-Id": "intruder"}

    body = open_client.get("/api/key/verify", headers=headers).json()

    assert body["permissions"] == []


@pytest.mark.xfail(
    reason="X-Signature / X-Timestamp are declared Header(...) (required), so FastAPI "
           "returns 422 before verify_hmac can short-circuit on HMAC_ENABLED=false. "
           "Dev (HMAC_ENABLED=false) callers must still send dummy headers.",
    strict=False,
)
def test_disabled_hmac_does_not_require_headers(open_client):
    assert open_client.get("/api/key/verify").status_code == 200


@pytest.mark.xfail(
    reason="verify_hmac calls float(x_timestamp) unguarded, so a non-numeric "
           "timestamp raises ValueError -> 500 instead of 401.",
    strict=False,
)
def test_non_numeric_timestamp_is_rejected_with_401():
    client = TestClient(build_app(), raise_server_exceptions=False)
    headers = {**sign(), "X-Timestamp": "not-a-number"}

    assert client.get("/api/key/verify", headers=headers).status_code == 401