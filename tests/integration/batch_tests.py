"""
Integration tests for GET /api/batch/info (routers/batch.py).

Mocked: Salesforce bulk client (org info / limits), token manager.
Real:   ScanState / ObjectProgress models, HMAC verification (in the auth tests).
"""

import hashlib
import hmac
import time
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from auth.hmac_auth import verify_hmac
from models.scan import ObjectProgress, ScanState, ScanStatus
from routers import batch

CORE_KEY = "core-key-" + "x" * 32
ENGINEER_KEY = "engineer-key-" + "y" * 32

ORG_INFO = {
    "id": "00Dxx0000001gPL",
    "name": "Glynac Production",
    "instance_url": "https://glynac.my.salesforce.com",
    "api_version": "59.0",
    "sandbox": False,
}
ORG_LIMITS = {"DailyApiRequests": {"Max": 15000, "Remaining": 12340}}


def sign(body="", key=CORE_KEY, key_id="core", timestamp=None):
    ts = str(time.time() if timestamp is None else timestamp)
    sig = hmac.new(key.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return {"X-Signature": sig, "X-Timestamp": ts, "X-Key-Id": key_id}


def scan_with(scan_id, status, **states):
    """Build a ScanState whose objects are in the given job states,
    e.g. scan_with("s1", ScanStatus.IN_PROGRESS, Contact="InProgress")."""
    s = ScanState(scan_id=scan_id, org_id="org-1")
    s.status = status
    s.progress = {
        obj: ObjectProgress(sf_job_id=f"{scan_id}-{obj}", state=state)
        for obj, state in states.items()
    }
    return s


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def bulk_client():
    bc = MagicMock()
    bc.get_org_info.return_value = dict(ORG_INFO)
    bc.get_org_limits.return_value = {k: dict(v) for k, v in ORG_LIMITS.items()}
    return bc


@pytest.fixture
def token_manager():
    tm = MagicMock()
    tm.seconds_until_expiry.return_value = 4823
    return tm


@pytest.fixture
def scan_store():
    store = MagicMock()
    store.list_all.return_value = []
    return store


@pytest.fixture
def app(bulk_client, token_manager, scan_store):
    app = FastAPI()
    app.include_router(batch.router)
    app.dependency_overrides[verify_hmac] = lambda: None
    app.state.bulk_client = bulk_client
    app.state.token_manager = token_manager
    app.state.scan_store = scan_store
    app.state.settings = MagicMock(HMAC_ENABLED=False)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def secured_client(bulk_client, token_manager, scan_store):
    app = FastAPI()

    @app.middleware("http")
    async def capture_raw_body(request: Request, call_next):
        request.state.raw_body = await request.body()
        return await call_next(request)

    app.include_router(batch.router)
    app.state.bulk_client = bulk_client
    app.state.token_manager = token_manager
    app.state.scan_store = scan_store
    app.state.settings = MagicMock(
        HMAC_ENABLED=True,
        HMAC_SECRET_KEY_CORE=CORE_KEY,
        HMAC_SECRET_KEY_ENGINEER=ENGINEER_KEY,
        HMAC_SIGNATURE_MAX_AGE=300,
    )
    return TestClient(app)


# ===========================================================================
# Happy path
# ===========================================================================
def test_batch_info_full_response(client):
    resp = client.get("/api/batch/info")
    body = resp.json()

    assert resp.status_code == 200
    assert body["org"] == ORG_INFO
    assert body["api_limits"]["daily_api_requests"] == {"max": 15000, "remaining": 12340}
    assert body["api_limits"]["bulk_api_jobs"] == {"active": 0, "max_concurrent": 100}
    assert body["token_expires_in_seconds"] == 4823


def test_batch_info_response_shape(client):
    body = client.get("/api/batch/info").json()

    assert set(body) == {"org", "api_limits", "token_expires_in_seconds"}
    assert set(body["api_limits"]) == {"daily_api_requests", "bulk_api_jobs"}


def test_batch_info_max_concurrent_matches_salesforce_hard_limit():
    assert batch.MAX_CONCURRENT_JOBS == 100


def test_batch_info_reads_live_token_expiry(client, token_manager):
    token_manager.seconds_until_expiry.return_value = 12
    assert client.get("/api/batch/info").json()["token_expires_in_seconds"] == 12

    token_manager.seconds_until_expiry.return_value = 0
    assert client.get("/api/batch/info").json()["token_expires_in_seconds"] == 0


def test_batch_info_reflects_remaining_quota(client, bulk_client):
    bulk_client.get_org_limits.return_value = {"DailyApiRequests": {"Max": 15000, "Remaining": 0}}

    daily = client.get("/api/batch/info").json()["api_limits"]["daily_api_requests"]

    assert daily == {"max": 15000, "remaining": 0}


# ===========================================================================
# Active-job counting
# ===========================================================================
def test_active_jobs_counts_upload_complete_and_in_progress(client, scan_store):
    scan_store.list_all.return_value = [
        scan_with("s1", ScanStatus.IN_PROGRESS,
                  Contact="UploadComplete", Account="InProgress", Opportunity="JobComplete"),
    ]

    active = client.get("/api/batch/info").json()["api_limits"]["bulk_api_jobs"]["active"]

    assert active == 2


def test_active_jobs_sums_across_scans(client, scan_store):
    scan_store.list_all.return_value = [
        scan_with("s1", ScanStatus.IN_PROGRESS, Contact="InProgress", Account="UploadComplete"),
        scan_with("s2", ScanStatus.IN_PROGRESS, Opportunity="InProgress"),
    ]

    active = client.get("/api/batch/info").json()["api_limits"]["bulk_api_jobs"]["active"]

    assert active == 3


@pytest.mark.parametrize("status", [ScanStatus.COMPLETE, ScanStatus.FAILED, ScanStatus.CANCELLED])
def test_active_jobs_ignores_scans_that_are_not_in_progress(client, scan_store, status):
    scan_store.list_all.return_value = [scan_with("s1", status, Contact="InProgress")]

    active = client.get("/api/batch/info").json()["api_limits"]["bulk_api_jobs"]["active"]

    assert active == 0


@pytest.mark.parametrize("state", ["JobComplete", "Failed", "Aborted"])
def test_active_jobs_ignores_terminal_job_states(client, scan_store, state):
    scan_store.list_all.return_value = [scan_with("s1", ScanStatus.IN_PROGRESS, Contact=state)]

    active = client.get("/api/batch/info").json()["api_limits"]["bulk_api_jobs"]["active"]

    assert active == 0


def test_active_jobs_handles_scan_with_no_objects(client, scan_store):
    scan_store.list_all.return_value = [scan_with("s1", ScanStatus.IN_PROGRESS)]

    active = client.get("/api/batch/info").json()["api_limits"]["bulk_api_jobs"]["active"]

    assert active == 0


# ===========================================================================
# Salesforce limits edge cases / failures
# ===========================================================================
def test_missing_daily_api_requests_yields_nulls(client, bulk_client):
    bulk_client.get_org_limits.return_value = {}

    resp = client.get("/api/batch/info")

    assert resp.status_code == 200
    assert resp.json()["api_limits"]["daily_api_requests"] == {"max": None, "remaining": None}


def test_org_info_failure_returns_502(client, bulk_client):
    bulk_client.get_org_info.side_effect = Exception("INVALID_SESSION_ID")

    resp = client.get("/api/batch/info")

    assert resp.status_code == 502
    assert "Salesforce API error" in resp.json()["detail"]


def test_org_limits_failure_returns_502(client, bulk_client):
    bulk_client.get_org_limits.side_effect = Exception("REQUEST_LIMIT_EXCEEDED")

    resp = client.get("/api/batch/info")

    assert resp.status_code == 502
    assert "Salesforce API error" in resp.json()["detail"]


def test_salesforce_failure_does_not_touch_scan_store(client, bulk_client, scan_store):
    bulk_client.get_org_info.side_effect = Exception("down")

    client.get("/api/batch/info")

    scan_store.list_all.assert_not_called()


def test_batch_info_only_allows_get(client):
    assert client.post("/api/batch/info").status_code == 405


# ===========================================================================
# HMAC enforcement (real verify_hmac)
# ===========================================================================
def test_batch_info_requires_signature_headers(secured_client, bulk_client):
    assert secured_client.get("/api/batch/info").status_code == 422
    bulk_client.get_org_info.assert_not_called()


def test_batch_info_rejects_bad_signature(secured_client, bulk_client):
    resp = secured_client.get("/api/batch/info", headers=sign(key="wrong"))

    assert resp.status_code == 401
    bulk_client.get_org_info.assert_not_called()  # no Salesforce quota spent


def test_batch_info_accepts_core_key(secured_client):
    assert secured_client.get("/api/batch/info", headers=sign()).status_code == 200


def test_batch_info_accepts_engineer_key(secured_client):
    resp = secured_client.get("/api/batch/info", headers=sign(key=ENGINEER_KEY, key_id="engineer"))
    assert resp.status_code == 200