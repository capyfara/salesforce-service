"""
Integration tests for POST /api/maintenance/cleanup (routers/cleanup.py).

Three layers:
  1. Route wiring      - maintenance.cleanup_old_scans is mocked; we check args/response.
  2. Key enforcement   - real verify_hmac + require_key_id("engineer").
  3. End-to-end        - real maintenance service against an in-memory store.
                         (Assumes stale-ness is judged by updated_at/created_at.)
"""

import hashlib
import hmac
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from auth.hmac_auth import verify_hmac
from models.scan import ScanState, ScanStatus
from routers import cleanup
from services import maintenance

CORE_KEY = "core-key-" + "x" * 32
ENGINEER_KEY = "engineer-key-" + "y" * 32


def sign(body="", key=CORE_KEY, key_id="core", timestamp=None):
    ts = str(time.time() if timestamp is None else timestamp)
    sig = hmac.new(key.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return {"X-Signature": sig, "X-Timestamp": ts, "X-Key-Id": key_id}


class ScanStore:
    """Minimal in-memory store (same contract as the real one)."""

    def __init__(self):
        self._scans = {}
        self._lock = threading.RLock()

    @contextmanager
    def lock(self, scan_id):
        with self._lock:
            yield

    def create(self, scan):
        self._scans[scan.scan_id] = scan

    update = create

    def get(self, scan_id):
        return self._scans.get(scan_id)

    def delete(self, scan_id):
        self._scans.pop(scan_id, None)

    def list_all(self):
        return list(self._scans.values())


def aged_scan(scan_id, days_old, status=ScanStatus.COMPLETE):
    s = ScanState(scan_id=scan_id, org_id="org-1")
    s.status = status
    ts = datetime.now(timezone.utc) - timedelta(days=days_old)
    s.updated_at = ts
    if hasattr(s, "created_at"):
        s.created_at = ts
    return s


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def scan_store():
    return ScanStore()


@pytest.fixture
def fake_cleanup(monkeypatch):
    fake = MagicMock(return_value={"deleted": 3, "dry_run": False})
    monkeypatch.setattr(maintenance, "cleanup_old_scans", fake)
    return fake


@pytest.fixture
def app(scan_store):
    """HMAC bypassed and disabled: exercises the route body only."""
    app = FastAPI()
    app.include_router(cleanup.router)
    app.dependency_overrides[verify_hmac] = lambda: None
    app.state.scan_store = scan_store
    app.state.settings = MagicMock(HMAC_ENABLED=False, CLEANUP_DAYS=30)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def secured_client(scan_store):
    """Real HMAC enforcement."""
    app = FastAPI()

    @app.middleware("http")
    async def capture_raw_body(request: Request, call_next):
        request.state.raw_body = await request.body()
        return await call_next(request)

    app.include_router(cleanup.router)
    app.state.scan_store = scan_store
    app.state.settings = MagicMock(
        HMAC_ENABLED=True,
        HMAC_SECRET_KEY_CORE=CORE_KEY,
        HMAC_SECRET_KEY_ENGINEER=ENGINEER_KEY,
        HMAC_SIGNATURE_MAX_AGE=300,
        CLEANUP_DAYS=30,
    )
    return TestClient(app)


# ===========================================================================
# 1. Route wiring
# ===========================================================================
def test_cleanup_calls_service_with_settings_and_defaults(client, scan_store, fake_cleanup):
    resp = client.post("/api/maintenance/cleanup")

    assert resp.status_code == 200
    fake_cleanup.assert_called_once_with(scan_store, cleanup_days=30, dry_run=False)


def test_cleanup_dry_run_true_is_forwarded(client, scan_store, fake_cleanup):
    client.post("/api/maintenance/cleanup", params={"dry_run": "true"})
    fake_cleanup.assert_called_once_with(scan_store, cleanup_days=30, dry_run=True)


def test_cleanup_uses_configured_retention(client, app, scan_store, fake_cleanup):
    app.state.settings.CLEANUP_DAYS = 7

    client.post("/api/maintenance/cleanup")

    fake_cleanup.assert_called_once_with(scan_store, cleanup_days=7, dry_run=False)


def test_cleanup_merges_service_result_into_response(client, fake_cleanup):
    fake_cleanup.return_value = {"deleted": 5, "scan_ids": ["a", "b"], "dry_run": False}

    body = client.post("/api/maintenance/cleanup").json()

    assert body == {"success": True, "deleted": 5, "scan_ids": ["a", "b"], "dry_run": False}


def test_cleanup_rejects_invalid_dry_run_value(client, fake_cleanup):
    resp = client.post("/api/maintenance/cleanup", params={"dry_run": "banana"})

    assert resp.status_code == 422
    fake_cleanup.assert_not_called()


def test_cleanup_service_error_surfaces_as_500(app, fake_cleanup):
    fake_cleanup.side_effect = RuntimeError("db unavailable")
    client = TestClient(app, raise_server_exceptions=False)

    assert client.post("/api/maintenance/cleanup").status_code == 500


def test_cleanup_only_allows_post(client):
    assert client.get("/api/maintenance/cleanup").status_code == 405
    assert client.delete("/api/maintenance/cleanup").status_code == 405


# ===========================================================================
# 2. Key enforcement (engineer-only)
# ===========================================================================
def test_cleanup_engineer_key_allowed(secured_client, fake_cleanup):
    resp = secured_client.post(
        "/api/maintenance/cleanup", headers=sign(key=ENGINEER_KEY, key_id="engineer")
    )

    assert resp.status_code == 200
    fake_cleanup.assert_called_once()


def test_cleanup_core_key_forbidden(secured_client, fake_cleanup):
    resp = secured_client.post("/api/maintenance/cleanup", headers=sign())

    assert resp.status_code == 403
    assert "engineer" in resp.json()["detail"]
    fake_cleanup.assert_not_called()


def test_cleanup_missing_headers_rejected(secured_client, fake_cleanup):
    assert secured_client.post("/api/maintenance/cleanup").status_code == 422
    fake_cleanup.assert_not_called()


def test_cleanup_bad_signature_rejected(secured_client, fake_cleanup):
    resp = secured_client.post(
        "/api/maintenance/cleanup", headers=sign(key="wrong-key", key_id="engineer")
    )

    assert resp.status_code == 401
    fake_cleanup.assert_not_called()


def test_cleanup_core_key_cannot_claim_engineer_id(secured_client, fake_cleanup):
    """Signing with the core secret but sending X-Key-Id: engineer must fail."""
    resp = secured_client.post(
        "/api/maintenance/cleanup", headers=sign(key=CORE_KEY, key_id="engineer")
    )

    assert resp.status_code == 401
    fake_cleanup.assert_not_called()


def test_cleanup_expired_signature_rejected(secured_client, fake_cleanup):
    resp = secured_client.post(
        "/api/maintenance/cleanup",
        headers=sign(key=ENGINEER_KEY, key_id="engineer", timestamp=time.time() - 3600),
    )

    assert resp.status_code == 401
    fake_cleanup.assert_not_called()


# ===========================================================================
# 3. End-to-end with the real maintenance service
# ===========================================================================
def test_cleanup_deletes_only_scans_past_retention(client, scan_store):
    scan_store.create(aged_scan("old-1", days_old=60))
    scan_store.create(aged_scan("old-2", days_old=45))
    scan_store.create(aged_scan("recent", days_old=2))

    resp = client.post("/api/maintenance/cleanup")

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert scan_store.get("old-1") is None
    assert scan_store.get("old-2") is None
    assert scan_store.get("recent") is not None


def test_cleanup_dry_run_deletes_nothing(client, scan_store):
    scan_store.create(aged_scan("old-1", days_old=60))
    scan_store.create(aged_scan("recent", days_old=2))

    resp = client.post("/api/maintenance/cleanup", params={"dry_run": "true"})

    assert resp.status_code == 200
    assert scan_store.get("old-1") is not None
    assert scan_store.get("recent") is not None


def test_cleanup_on_empty_store_is_a_noop(client):
    resp = client.post("/api/maintenance/cleanup")

    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_cleanup_is_idempotent(client, scan_store):
    scan_store.create(aged_scan("old-1", days_old=60))

    first = client.post("/api/maintenance/cleanup")
    second = client.post("/api/maintenance/cleanup")

    assert first.status_code == second.status_code == 200
    assert scan_store.get("old-1") is None