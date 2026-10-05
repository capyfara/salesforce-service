"""
Integration tests for GET /api/statistics (routers/statistics.py).

The scan store is mocked (only list_all is used); ScanState / ObjectProgress are real.
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
from routers import statistics

CORE_KEY = "core-key-" + "x" * 32
ENGINEER_KEY = "engineer-key-" + "y" * 32


def sign(body="", key=CORE_KEY, key_id="core", timestamp=None):
    ts = str(time.time() if timestamp is None else timestamp)
    sig = hmac.new(key.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return {"X-Signature": sig, "X-Timestamp": ts, "X-Key-Id": key_id}


def make_scan(scan_id, status, **records_by_object):
    """make_scan("s1", ScanStatus.COMPLETE, Contact=100, Account=50)"""
    s = ScanState(scan_id=scan_id, org_id="org-1")
    s.status = status
    s.progress = {
        obj: ObjectProgress(sf_job_id=f"{scan_id}-{obj}", state="JobComplete", records_processed=n)
        for obj, n in records_by_object.items()
    }
    return s


def scan_count(body):
    """The count key is currently 'total scans' (with a space). Accept either
    spelling so these tests survive a rename."""
    return body["total_scans"] if "total_scans" in body else body["total scans"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def scan_store():
    store = MagicMock()
    store.list_all.return_value = []
    return store


@pytest.fixture
def app(scan_store):
    app = FastAPI()
    app.include_router(statistics.router)
    app.dependency_overrides[verify_hmac] = lambda: None
    app.state.scan_store = scan_store
    app.state.settings = MagicMock(HMAC_ENABLED=False)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def secured_client(scan_store):
    app = FastAPI()

    @app.middleware("http")
    async def capture_raw_body(request: Request, call_next):
        request.state.raw_body = await request.body()
        return await call_next(request)

    app.include_router(statistics.router)
    app.state.scan_store = scan_store
    app.state.settings = MagicMock(
        HMAC_ENABLED=True,
        HMAC_SECRET_KEY_CORE=CORE_KEY,
        HMAC_SECRET_KEY_ENGINEER=ENGINEER_KEY,
        HMAC_SIGNATURE_MAX_AGE=300,
    )
    return TestClient(app)


# ===========================================================================
# Aggregation
# ===========================================================================
def test_statistics_empty_store(client):
    resp = client.get("/api/statistics")
    body = resp.json()

    assert resp.status_code == 200
    assert scan_count(body) == 0
    assert body["by_status"] == {}
    assert body["total_records_extracted"] == 0


def test_statistics_counts_scans_by_status(client, scan_store):
    scan_store.list_all.return_value = [
        make_scan("a", ScanStatus.COMPLETE),
        make_scan("b", ScanStatus.COMPLETE),
        make_scan("c", ScanStatus.FAILED),
        make_scan("d", ScanStatus.IN_PROGRESS),
        make_scan("e", ScanStatus.CANCELLED),
    ]

    body = client.get("/api/statistics").json()

    assert scan_count(body) == 5
    assert body["by_status"] == {
        ScanStatus.COMPLETE.value: 2,
        ScanStatus.FAILED.value: 1,
        ScanStatus.IN_PROGRESS.value: 1,
        ScanStatus.CANCELLED.value: 1,
    }


def test_statistics_by_status_omits_statuses_with_no_scans(client, scan_store):
    scan_store.list_all.return_value = [make_scan("a", ScanStatus.COMPLETE)]

    by_status = client.get("/api/statistics").json()["by_status"]

    assert set(by_status) == {ScanStatus.COMPLETE.value}


def test_statistics_by_status_counts_sum_to_total(client, scan_store):
    scan_store.list_all.return_value = [
        make_scan("a", ScanStatus.COMPLETE),
        make_scan("b", ScanStatus.FAILED),
        make_scan("c", ScanStatus.FAILED),
    ]

    body = client.get("/api/statistics").json()

    assert sum(body["by_status"].values()) == scan_count(body)


def test_statistics_sums_records_across_objects_and_scans(client, scan_store):
    scan_store.list_all.return_value = [
        make_scan("a", ScanStatus.COMPLETE, Contact=100, Account=50),
        make_scan("b", ScanStatus.FAILED, Opportunity=25),
    ]

    body = client.get("/api/statistics").json()

    assert body["total_records_extracted"] == 175


def test_statistics_scan_without_objects_contributes_zero(client, scan_store):
    scan_store.list_all.return_value = [
        make_scan("a", ScanStatus.IN_PROGRESS),
        make_scan("b", ScanStatus.COMPLETE, Contact=10),
    ]

    body = client.get("/api/statistics").json()

    assert scan_count(body) == 2
    assert body["total_records_extracted"] == 10


def test_statistics_counts_records_from_non_complete_scans(client, scan_store):
    """Partial progress from failed/cancelled scans is still 'extracted'."""
    scan_store.list_all.return_value = [
        make_scan("a", ScanStatus.FAILED, Contact=40),
        make_scan("b", ScanStatus.CANCELLED, Account=60),
    ]

    assert client.get("/api/statistics").json()["total_records_extracted"] == 100


def test_statistics_reflects_live_store_between_calls(client, scan_store):
    scan_store.list_all.return_value = [make_scan("a", ScanStatus.COMPLETE, Contact=5)]
    first = client.get("/api/statistics").json()

    scan_store.list_all.return_value = [
        make_scan("a", ScanStatus.COMPLETE, Contact=5),
        make_scan("b", ScanStatus.COMPLETE, Contact=7),
    ]
    second = client.get("/api/statistics").json()

    assert scan_count(first) == 1 and first["total_records_extracted"] == 5
    assert scan_count(second) == 2 and second["total_records_extracted"] == 12


def test_statistics_only_allows_get(client):
    assert client.post("/api/statistics").status_code == 405


@pytest.mark.xfail(
    reason="Response key is 'total scans' (with a space) while sibling keys are "
           "snake_case (by_status, total_records_extracted).",
    strict=False,
)
def test_statistics_uses_snake_case_keys(client):
    assert "total_scans" in client.get("/api/statistics").json()


# ===========================================================================
# HMAC enforcement (real verify_hmac)
# ===========================================================================
def test_statistics_requires_signature_headers(secured_client, scan_store):
    assert secured_client.get("/api/statistics").status_code == 422
    scan_store.list_all.assert_not_called()


def test_statistics_rejects_bad_signature(secured_client, scan_store):
    resp = secured_client.get("/api/statistics", headers=sign(key="wrong"))

    assert resp.status_code == 401
    scan_store.list_all.assert_not_called()


def test_statistics_accepts_core_key(secured_client):
    assert secured_client.get("/api/statistics", headers=sign()).status_code == 200


def test_statistics_accepts_engineer_key(secured_client):
    resp = secured_client.get("/api/statistics", headers=sign(key=ENGINEER_KEY, key_id="engineer"))
    assert resp.status_code == 200