"""
Integration tests for the /api/scan/* endpoints and extraction_service.

Covers: start, list, status, cancel, resume, remove, HMAC auth,
run_scan_background behaviour, and cancel-vs-background races.

These tests use a REAL (in-memory) ScanStore so the locking behavior
is actually exercised, not mocked away. Only the Salesforce Bulk API
client, MinIO client and Core Service client are mocked, since those
are the external boundaries.
"""

import hashlib
import hmac
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from auth.hmac_auth import verify_hmac
from models.scan import ObjectProgress, ScanState, ScanStatus
from routers import scan
import services.extraction_service as extraction_service


# ---------------------------------------------------------------------------
# Minimal real ScanStore
# ---------------------------------------------------------------------------
class ScanStore:
    def __init__(self):
        self._scans: dict[str, ScanState] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.RLock()

    def _get_lock(self, scan_id: str) -> threading.RLock:
        with self._locks_guard:
            if scan_id not in self._locks:
                self._locks[scan_id] = threading.RLock()
            return self._locks[scan_id]

    @contextmanager
    def lock(self, scan_id: str):
        lock = self._get_lock(scan_id)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def create(self, scan: ScanState):
        with self.lock(scan.scan_id):
            self._scans[scan.scan_id] = scan

    def get(self, scan_id: str) -> ScanState | None:
        with self.lock(scan_id):
            return self._scans.get(scan_id)

    def update(self, scan: ScanState):
        with self.lock(scan.scan_id):
            self._scans[scan.scan_id] = scan

    def delete(self, scan_id: str):
        with self.lock(scan_id):
            self._scans.pop(scan_id, None)

    def list_all(self):
        with self._locks_guard:
            return list(self._scans.values())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def job_result(state="JobComplete", processed=10, failed=0, error=None):
    """Fake return value for bulk_client.poll_until_complete."""
    r = MagicMock()
    r.state = state
    r.records_processed = processed
    r.records_failed = failed
    r.error_message = error
    return r


def make_scan(scan_store, scan_id="scan-1", org_id="org-1",
              status=ScanStatus.IN_PROGRESS,
              progress=None, last_modified_after=None):
    """Seed a scan directly into the store, bypassing /scan/start."""
    scan_obj = ScanState(scan_id=scan_id, org_id=org_id)
    scan_obj.status = status
    scan_obj.progress = progress or {}
    scan_obj.updated_at = datetime.now(timezone.utc)
    if hasattr(scan_obj, "last_modified_after"):
        scan_obj.last_modified_after = last_modified_after
    scan_store.create(scan_obj)
    return scan_obj


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def scan_store():
    return ScanStore()


@pytest.fixture
def bulk_client():
    """MagicMock bulk client with sane happy-path defaults. Individual tests
    override return_value / side_effect on the methods they care about."""
    client = MagicMock()
    client.create_query_job.return_value = "750fakejobid000001"
    client.get_job_info.return_value = {"state": "Aborted"}
    client.poll_until_complete.return_value = job_result()
    # side_effect (not return_value) so every call gets a fresh iterator
    client.iter_results.side_effect = lambda *a, **k: iter([])
    return client


@pytest.fixture
def minio_client():
    client = MagicMock()
    client.upload_records.return_value = "s3://fake-bucket/fake/path/page_001.parquet"
    return client


@pytest.fixture
def core_client():
    return MagicMock()


@pytest.fixture
def app(scan_store, bulk_client, minio_client, core_client):
    app = FastAPI()
    app.include_router(scan.router)
    # HMAC is exercised separately (see HMAC section); bypass it here.
    app.dependency_overrides[verify_hmac] = lambda: None
    app.state.scan_store = scan_store
    app.state.bulk_client = bulk_client
    app.state.minio_client = minio_client
    app.state.core_client = core_client  # start/resume read this from app.state
    app.state.settings = MagicMock(SF_MAX_JOB_TIMEOUT_HOURS=2, HMAC_ENABLED=False)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def stub_soql(monkeypatch):
    """Replace build_soql so endpoint tests don't depend on SUPPORTED_OBJECTS."""
    monkeypatch.setattr(
        extraction_service, "build_soql", lambda obj, filt: f"SELECT Id FROM {obj}"
    )


# ===========================================================================
# START TESTS
# ===========================================================================

def test_start_scan_creates_one_job_per_object(client, scan_store, bulk_client, stub_soql):
    bulk_client.create_query_job.side_effect = lambda soql: "job-" + soql.rsplit(" ", 1)[-1]

    resp = client.post("/api/scan/start", json={
        "scan_id": "scan-new", "org_id": "org-1", "objects": ["Contact", "Account"],
    })
    body = resp.json()

    assert resp.status_code == 202
    assert body["success"] is True
    assert body["status"] == "started"
    assert body["scan_id"] == "scan-new"
    assert body["jobs"] == {"Contact": "job-Contact", "Account": "job-Account"}
    assert "2 Bulk API jobs created" in body["message"]
    assert bulk_client.create_query_job.call_count == 2

    # TestClient runs background tasks before returning; with the default
    # happy-path mocks the scan should have finished.
    stored = scan_store.get("scan-new")
    assert stored.org_id == "org-1"
    assert stored.status == ScanStatus.COMPLETE


def test_start_scan_rejects_unsupported_object(client, scan_store, bulk_client):
    resp = client.post("/api/scan/start", json={
        "scan_id": "scan-bad", "org_id": "org-1", "objects": ["NotARealObject"],
    })

    assert resp.status_code == 400
    assert "Unsupported object" in resp.json()["detail"]
    bulk_client.create_query_job.assert_not_called()
    assert scan_store.get("scan-bad") is None  # nothing persisted


@pytest.mark.parametrize("payload", [
    {"org_id": "org-1", "objects": ["Contact"]},                       # no scan_id
    {"scan_id": "s", "objects": ["Contact"]},                          # no org_id
    {"scan_id": "s", "org_id": "org-1"},                               # no objects
    {"scan_id": "s", "org_id": "org-1", "objects": "Contact"},         # wrong type
])
def test_start_scan_validates_payload(client, payload):
    assert client.post("/api/scan/start", json=payload).status_code == 422


def test_start_scan_passes_last_modified_filter(client, scan_store, monkeypatch):
    captured = {}

    def fake_build_soql(obj, filt):
        captured[obj] = filt
        return f"SELECT Id FROM {obj}"

    monkeypatch.setattr(extraction_service, "build_soql", fake_build_soql)

    client.post("/api/scan/start", json={
        "scan_id": "scan-inc", "org_id": "org-1", "objects": ["Contact"],
        "filters": {"last_modified_after": "2026-04-01T00:00:00Z"},
    })

    assert captured["Contact"] == "2026-04-01T00:00:00Z"
    stored = scan_store.get("scan-inc")
    assert getattr(stored, "last_modified_after") == "2026-04-01T00:00:00Z"


def test_start_scan_uploads_every_page_and_cleans_up(
    client, scan_store, bulk_client, minio_client, stub_soql
):
    bulk_client.create_query_job.return_value = "job-contact"
    bulk_client.poll_until_complete.return_value = job_result(processed=3)
    bulk_client.iter_results.side_effect = lambda *a, **k: iter([
        [{"Id": "001"}, {"Id": "002"}],
        [{"Id": "003"}],
    ])
    minio_client.upload_records.side_effect = (
        lambda **kw: f"s3://b/{kw['object_name']}/page_{kw['page_num']:03d}.parquet"
    )

    client.post("/api/scan/start", json={
        "scan_id": "scan-pages", "org_id": "org-1", "objects": ["Contact"],
    })

    assert minio_client.upload_records.call_count == 2
    page_nums = [c.kwargs["page_num"] for c in minio_client.upload_records.call_args_list]
    assert page_nums == [1, 2]
    for c in minio_client.upload_records.call_args_list:
        assert c.kwargs["org_id"] == "org-1"
        assert c.kwargs["scan_id"] == "scan-pages"
        assert c.kwargs["object_name"] == "Contact"

    contact = scan_store.get("scan-pages").progress["Contact"]
    assert contact.pages_downloaded == 2
    assert contact.records_processed == 3
    assert contact.minio_path == [
        "s3://b/Contact/page_001.parquet",
        "s3://b/Contact/page_002.parquet",
    ]
    bulk_client.delete_job.assert_called_once_with("job-contact")


# ===========================================================================
# LIST TESTS
# ===========================================================================

def test_list_scans_empty(client):
    resp = client.get("/api/scan/list")
    assert resp.status_code == 200
    assert resp.json() == {"scans": [], "count": 0}


def test_list_scans_returns_all(client, scan_store):
    make_scan(scan_store, scan_id="a")
    make_scan(scan_store, scan_id="b")

    body = client.get("/api/scan/list").json()

    assert body["count"] == 2
    assert {s["scan_id"] for s in body["scans"]} == {"a", "b"}


def test_list_scans_filters_by_status(client, scan_store):
    make_scan(scan_store, scan_id="a", status=ScanStatus.FAILED)
    make_scan(scan_store, scan_id="b", status=ScanStatus.IN_PROGRESS)
    make_scan(scan_store, scan_id="c", status=ScanStatus.FAILED)

    body = client.get("/api/scan/list", params={"status": ScanStatus.FAILED.value}).json()

    assert body["count"] == 2
    assert {s["scan_id"] for s in body["scans"]} == {"a", "c"}


def test_list_scans_filters_by_org(client, scan_store):
    make_scan(scan_store, scan_id="a", org_id="org-1")
    make_scan(scan_store, scan_id="b", org_id="org-2")

    body = client.get("/api/scan/list", params={"org_id": "org-2"}).json()

    assert body["count"] == 1
    assert body["scans"][0]["scan_id"] == "b"


def test_list_scans_combines_filters(client, scan_store):
    make_scan(scan_store, scan_id="a", org_id="org-1", status=ScanStatus.FAILED)
    make_scan(scan_store, scan_id="b", org_id="org-2", status=ScanStatus.FAILED)
    make_scan(scan_store, scan_id="c", org_id="org-1", status=ScanStatus.IN_PROGRESS)

    body = client.get("/api/scan/list", params={
        "status": ScanStatus.FAILED.value, "org_id": "org-1",
    }).json()

    assert [s["scan_id"] for s in body["scans"]] == ["a"]


def test_list_scans_no_match_returns_empty(client, scan_store):
    make_scan(scan_store, scan_id="a", org_id="org-1")
    body = client.get("/api/scan/list", params={"org_id": "nope"}).json()
    assert body == {"scans": [], "count": 0}


# ===========================================================================
# STATUS TESTS
# ===========================================================================

def test_status_not_found(client):
    assert client.get("/api/scan/does-not-exist/status").status_code == 404


def test_status_reports_progress_and_totals(client, scan_store):
    make_scan(
        scan_store,
        progress={
            "Contact": ObjectProgress(sf_job_id="j1", state="JobComplete", records_processed=100),
            "Account": ObjectProgress(sf_job_id="j2", state="Failed", error="boom"),
            "Opportunity": ObjectProgress(sf_job_id="j3", state="InProgress"),
        },
    )

    resp = client.get("/api/scan/scan-1/status")
    body = resp.json()

    assert resp.status_code == 200
    assert body["scan_id"] == "scan-1"
    assert body["org_id"] == "org-1"
    assert body["progress"]["Contact"]["sf_job_id"] == "j1"
    assert body["progress"]["Account"]["error"] == "boom"
    assert body["totals"] == {
        "objects_total": 3,
        "objects_complete": 1,
        "objects_failed": 1,
        "records_extracted": 100,
    }


def test_status_totals_for_scan_with_no_objects(client, scan_store):
    make_scan(scan_store, progress={})
    totals = client.get("/api/scan/scan-1/status").json()["totals"]
    assert totals == {
        "objects_total": 0, "objects_complete": 0,
        "objects_failed": 0, "records_extracted": 0,
    }


# ===========================================================================
# CANCEL (scan) TESTS
# ===========================================================================

def test_cancel_scan_not_found(client):
    resp = client.post("/api/scan/does-not-exist/cancel")
    assert resp.status_code == 404


def test_cancel_aborts_in_progress_jobs(client, scan_store, bulk_client):
    """Jobs still running should be aborted and confirmed via GET."""
    make_scan(
        scan_store,
        progress={
            "Contact": ObjectProgress(sf_job_id="job-contact", state="InProgress"),
            "Account": ObjectProgress(sf_job_id="job-account", state="InProgress"),
        },
    )
    bulk_client.get_job_info.return_value = {"state": "Aborted"}

    resp = client.post("/api/scan/scan-1/cancel")
    body = resp.json()

    assert resp.status_code == 200
    assert body["success"] is True
    assert body["status"] == "cancelled"
    assert set(body["jobs_aborted"]) == {"job-contact", "job-account"}
    assert body["jobs_already_complete"] == []
    assert body["jobs_failed_to_abort"] == []

    assert bulk_client.abort_job.call_count == 2
    bulk_client.abort_job.assert_any_call("job-contact")
    bulk_client.abort_job.assert_any_call("job-account")

    # Store reflects confirmed state, not an assumed one
    stored = scan_store.get("scan-1")
    assert stored.status == ScanStatus.CANCELLED
    assert stored.progress["Contact"].state == "Aborted"
    assert stored.progress["Account"].state == "Aborted"


def test_cancel_leaves_already_complete_jobs_alone(client, scan_store, bulk_client):
    """Completed jobs should never be aborted — just reported."""
    make_scan(
        scan_store,
        progress={
            "Contact": ObjectProgress(sf_job_id="job-contact", state="JobComplete",
                                      records_processed=100),
            "Account": ObjectProgress(sf_job_id="job-account", state="InProgress"),
        },
    )
    bulk_client.get_job_info.return_value = {"state": "Aborted"}

    resp = client.post("/api/scan/scan-1/cancel")
    body = resp.json()

    assert body["success"] is True
    assert body["jobs_already_complete"] == ["job-contact"]
    assert body["jobs_aborted"] == ["job-account"]

    called_ids = [c.args[0] for c in bulk_client.abort_job.call_args_list]
    assert "job-contact" not in called_ids

    stored = scan_store.get("scan-1")
    assert stored.progress["Contact"].state == "JobComplete"  # untouched
    assert stored.progress["Contact"].records_processed == 100  # untouched


def test_cancel_reports_failure_when_abort_not_confirmed(client, scan_store, bulk_client):
    """If Salesforce doesn't confirm Aborted, success must be False and the
    job's real (uncertain) state should be reflected, not assumed."""
    make_scan(
        scan_store,
        progress={"Contact": ObjectProgress(sf_job_id="job-contact", state="InProgress")},
    )
    # abort_job "succeeds" but SF reports it actually completed in the interim.
    bulk_client.get_job_info.return_value = {"state": "JobComplete"}

    resp = client.post("/api/scan/scan-1/cancel")
    body = resp.json()

    assert body["success"] is False
    assert body["status"] == "failed"
    assert body["jobs_aborted"] == []
    assert body["jobs_failed_to_abort"] == ["job-contact"]
    assert "may still be running" in body["message"]

    stored = scan_store.get("scan-1")
    assert stored.progress["Contact"].state == "JobComplete"  # trusts SF, not intent
    assert stored.status != ScanStatus.CANCELLED


def test_cancel_handles_abort_call_exception(client, scan_store, bulk_client):
    """Network/API errors during abort should land in jobs_failed_to_abort,
    not crash the endpoint or silently claim success."""
    make_scan(
        scan_store,
        progress={"Contact": ObjectProgress(sf_job_id="job-contact", state="InProgress")},
    )
    bulk_client.abort_job.side_effect = Exception("connection reset")

    resp = client.post("/api/scan/scan-1/cancel")
    body = resp.json()

    assert resp.status_code == 200
    assert body["success"] is False
    assert body["jobs_failed_to_abort"] == ["job-contact"]
    # A failed abort must not be recorded as a cancelled scan
    assert scan_store.get("scan-1").status != ScanStatus.CANCELLED


def test_cancel_partial_failure_keeps_scan_not_cancelled(client, scan_store, bulk_client):
    """One job aborts, one errors: reported as partial, scan NOT marked cancelled."""
    make_scan(
        scan_store,
        progress={
            "Contact": ObjectProgress(sf_job_id="job-contact", state="InProgress"),
            "Account": ObjectProgress(sf_job_id="job-account", state="InProgress"),
        },
    )

    def abort(job_id):
        if job_id == "job-account":
            raise Exception("timeout")

    bulk_client.abort_job.side_effect = abort
    bulk_client.get_job_info.return_value = {"state": "Aborted"}

    body = client.post("/api/scan/scan-1/cancel").json()

    assert body["jobs_aborted"] == ["job-contact"]
    assert body["jobs_failed_to_abort"] == ["job-account"]
    assert scan_store.get("scan-1").status != ScanStatus.CANCELLED


def test_cancel_is_idempotent(client, scan_store, bulk_client):
    """Calling cancel twice should short-circuit the second time and not
    re-issue abort calls."""
    make_scan(
        scan_store,
        progress={"Contact": ObjectProgress(sf_job_id="job-contact", state="InProgress")},
    )
    bulk_client.get_job_info.return_value = {"state": "Aborted"}

    first = client.post("/api/scan/scan-1/cancel")
    assert first.json()["success"] is True
    assert bulk_client.abort_job.call_count == 1

    second = client.post("/api/scan/scan-1/cancel")
    assert second.json()["message"] == "Already cancelled"
    assert bulk_client.abort_job.call_count == 1


def test_cancel_does_not_touch_already_failed_or_aborted_objects(client, scan_store, bulk_client):
    make_scan(
        scan_store,
        progress={
            "Contact": ObjectProgress(sf_job_id="job-contact", state="Failed"),
            "Account": ObjectProgress(sf_job_id="job-account", state="Aborted"),
        },
    )
    body = client.post("/api/scan/scan-1/cancel").json()

    assert body["jobs_aborted"] == []
    assert body["jobs_already_complete"] == []
    assert bulk_client.abort_job.call_count == 0


# ===========================================================================
# RESUME TESTS
# ===========================================================================

def test_resume_not_found(client):
    resp = client.post("/api/scan/does-not-exist/resume")
    assert resp.status_code == 404


@pytest.mark.parametrize("status", [ScanStatus.COMPLETE, ScanStatus.IN_PROGRESS, ScanStatus.CANCELLED])
def test_resume_rejects_non_failed_scan(client, scan_store, bulk_client, status):
    make_scan(scan_store, status=status,
              progress={"Contact": ObjectProgress(sf_job_id="job-1", state="Failed")})

    resp = client.post("/api/scan/scan-1/resume")

    assert resp.status_code == 400
    assert "Only failed scans can be resumed" in resp.json()["detail"]
    bulk_client.create_query_job.assert_not_called()


def test_resume_rejects_when_nothing_incomplete(client, scan_store):
    """Edge case: status says FAILED but every object is complete."""
    make_scan(
        scan_store,
        status=ScanStatus.FAILED,
        progress={"Contact": ObjectProgress(sf_job_id="job-1", state="JobComplete")},
    )
    resp = client.post("/api/scan/scan-1/resume")
    assert resp.status_code == 400
    assert "No incomplete objects" in resp.json()["detail"]


def test_resume_recreates_jobs_only_for_incomplete_objects(client, scan_store, bulk_client, stub_soql):
    """Completed objects are left untouched; failed objects get new SF jobs."""
    make_scan(
        scan_store,
        status=ScanStatus.FAILED,
        last_modified_after="2026-01-01T00:00:00Z",
        progress={
            "Contact": ObjectProgress(sf_job_id="job-contact-old", state="JobComplete",
                                      records_processed=50, minio_path=["s3://.../page_001.parquet"]),
            "Account": ObjectProgress(sf_job_id="job-account-old", state="Failed",
                                      error="REQUEST_LIMIT_EXCEEDED"),
        },
    )
    bulk_client.create_query_job.side_effect = ["job-account-new"]
    bulk_client.poll_until_complete.return_value = job_result(processed=20)

    resp = client.post("/api/scan/scan-1/resume")
    body = resp.json()

    assert resp.status_code == 200
    assert body["success"] is True
    assert body["resumed_objects"] == ["Account"]

    stored = scan_store.get("scan-1")
    assert stored.status == ScanStatus.COMPLETE

    contact = stored.progress["Contact"]
    assert contact.sf_job_id == "job-contact-old"
    assert contact.state == "JobComplete"
    assert contact.records_processed == 50
    assert contact.minio_path == ["s3://.../page_001.parquet"]

    account = stored.progress["Account"]
    assert account.sf_job_id == "job-account-new"
    assert account.state == "JobComplete"
    assert account.records_processed == 20
    assert account.error is None

    bulk_client.create_query_job.assert_called_once_with("SELECT Id FROM Account")
    # Background task must only poll the resumed job, not the finished one
    polled = [c.args[0] for c in bulk_client.poll_until_complete.call_args_list]
    assert polled == ["job-account-new"]


def test_resume_reuses_original_filter(client, scan_store, bulk_client, monkeypatch):
    """The stored last_modified_after must be passed back into build_soql
    so resume doesn't silently re-extract the full object."""
    captured = {}

    def fake_build_soql(obj, filt):
        captured[obj] = filt
        return f"SELECT Id FROM {obj}"

    monkeypatch.setattr(extraction_service, "build_soql", fake_build_soql)

    make_scan(
        scan_store,
        status=ScanStatus.FAILED,
        last_modified_after="2026-04-01T00:00:00Z",
        progress={"Opportunity": ObjectProgress(sf_job_id="job-old", state="Failed")},
    )
    bulk_client.create_query_job.return_value = "job-new"

    client.post("/api/scan/scan-1/resume")

    assert captured["Opportunity"] == "2026-04-01T00:00:00Z"


def test_resume_can_fail_again(client, scan_store, bulk_client, stub_soql):
    """A resumed job that fails again leaves the scan FAILED (and resumable)."""
    make_scan(
        scan_store,
        status=ScanStatus.FAILED,
        progress={"Account": ObjectProgress(sf_job_id="job-old", state="Failed")},
    )
    bulk_client.create_query_job.return_value = "job-new"
    bulk_client.poll_until_complete.return_value = job_result(
        state="Failed", processed=0, error="INVALID_FIELD"
    )

    client.post("/api/scan/scan-1/resume")

    stored = scan_store.get("scan-1")
    assert stored.status == ScanStatus.FAILED
    assert stored.progress["Account"].sf_job_id == "job-new"
    assert stored.progress["Account"].error == "INVALID_FIELD"


# ===========================================================================
# REMOVE TESTS
# ===========================================================================

def test_remove_scan_not_found(client):
    assert client.delete("/api/scan/does-not-exist/remove").status_code == 404


def test_remove_scan_deletes_record(client, scan_store):
    make_scan(scan_store)

    resp = client.delete("/api/scan/scan-1/remove")

    assert resp.status_code == 200
    assert resp.json() == {"message": "Scan removed successfully"}
    assert scan_store.get("scan-1") is None


def test_remove_scan_leaves_other_scans(client, scan_store):
    make_scan(scan_store, scan_id="keep")
    make_scan(scan_store, scan_id="drop")

    client.delete("/api/scan/drop/remove")

    assert scan_store.get("keep") is not None
    assert scan_store.get("drop") is None


def test_remove_scan_reports_500_when_delete_does_not_stick(client, scan_store, monkeypatch):
    make_scan(scan_store)
    monkeypatch.setattr(scan_store, "delete", lambda scan_id: None)  # delete silently fails

    resp = client.delete("/api/scan/scan-1/remove")

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Failed to remove scan"


# ===========================================================================
# extraction_service: resolve_objects / build_soql
# ===========================================================================

def test_resolve_objects_dedupes_preserving_order():
    assert extraction_service.resolve_objects(["Account", "Contact", "Account"]) == ["Account", "Contact"]


def test_resolve_objects_rejects_unknown():
    with pytest.raises(ValueError, match="Unsupported object: Bogus"):
        extraction_service.resolve_objects(["Contact", "Bogus"])


def test_resolve_objects_expands_activity_alias(monkeypatch):
    # Task/Event aren't in SUPPORTED_OBJECTS yet; register them for this test.
    monkeypatch.setitem(extraction_service._BY_NAME, "Task", object())
    monkeypatch.setitem(extraction_service._BY_NAME, "Event", object())

    assert extraction_service.resolve_objects(["Contact", "Activity"]) == ["Contact", "Task", "Event"]


def test_build_soql_full_extraction_has_no_where_clause():
    soql = extraction_service.build_soql("Contact", None)
    assert soql.startswith("SELECT Id, Name")
    assert "FROM Contact" in soql
    assert "WHERE" not in soql
    assert soql.endswith("ORDER BY LastModifiedDate ASC")


def test_build_soql_incremental_adds_filter():
    soql = extraction_service.build_soql("Account", "2026-04-01T00:00:00Z")
    assert "WHERE LastModifiedDate >= 2026-04-01T00:00:00Z" in soql
    assert soql.index("WHERE") < soql.index("ORDER BY")


# ===========================================================================
# run_scan_background
# ===========================================================================

def run_bg(bulk_client, scan_store, minio_client, core_client, scan_id="scan-1", timeout=7200):
    extraction_service.run_scan_background(
        bulk_client, scan_store, scan_id, minio_client, timeout, core_client
    )


def test_background_missing_scan_is_noop(scan_store, bulk_client, minio_client, core_client):
    run_bg(bulk_client, scan_store, minio_client, core_client, scan_id="ghost")
    bulk_client.poll_until_complete.assert_not_called()


def test_background_polls_with_configured_timeout(scan_store, bulk_client, minio_client, core_client):
    make_scan(scan_store, progress={"Contact": ObjectProgress(sf_job_id="job-1", state="UploadComplete")})

    run_bg(bulk_client, scan_store, minio_client, core_client, timeout=3600)

    bulk_client.poll_until_complete.assert_called_once_with("job-1", 3600)


def test_background_skips_already_complete_objects(scan_store, bulk_client, minio_client, core_client):
    make_scan(scan_store, progress={
        "Contact": ObjectProgress(sf_job_id="job-done", state="JobComplete"),
        "Account": ObjectProgress(sf_job_id="job-todo", state="UploadComplete"),
    })

    run_bg(bulk_client, scan_store, minio_client, core_client)

    polled = [c.args[0] for c in bulk_client.poll_until_complete.call_args_list]
    assert polled == ["job-todo"]
    assert scan_store.get("scan-1").status == ScanStatus.COMPLETE


def test_background_all_jobs_complete_marks_scan_complete(scan_store, bulk_client, minio_client, core_client):
    make_scan(scan_store, progress={
        "Contact": ObjectProgress(sf_job_id="j1", state="UploadComplete"),
        "Account": ObjectProgress(sf_job_id="j2", state="UploadComplete"),
    })
    bulk_client.poll_until_complete.return_value = job_result(processed=7)

    run_bg(bulk_client, scan_store, minio_client, core_client)

    stored = scan_store.get("scan-1")
    assert stored.status == ScanStatus.COMPLETE
    assert stored.progress["Contact"].records_processed == 7
    assert stored.progress["Account"].records_processed == 7
    assert bulk_client.delete_job.call_count == 2


def test_background_failed_job_records_error_and_skips_download(
    scan_store, bulk_client, minio_client, core_client
):
    make_scan(scan_store, progress={"Contact": ObjectProgress(sf_job_id="j1", state="UploadComplete")})
    bulk_client.poll_until_complete.return_value = job_result(
        state="Failed", processed=0, error="MALFORMED_QUERY"
    )

    run_bg(bulk_client, scan_store, minio_client, core_client)

    stored = scan_store.get("scan-1")
    assert stored.status == ScanStatus.FAILED
    assert stored.progress["Contact"].state == "Failed"
    assert stored.progress["Contact"].error == "MALFORMED_QUERY"
    bulk_client.iter_results.assert_not_called()
    minio_client.upload_records.assert_not_called()
    bulk_client.delete_job.assert_not_called()


def test_background_poll_exception_marks_object_failed_and_continues(
    scan_store, bulk_client, minio_client, core_client
):
    """One object blowing up must not stop the remaining objects."""
    make_scan(scan_store, progress={
        "Contact": ObjectProgress(sf_job_id="j1", state="UploadComplete"),
        "Account": ObjectProgress(sf_job_id="j2", state="UploadComplete"),
    })

    def poll(job_id, timeout):
        if job_id == "j1":
            raise RuntimeError("socket closed")
        return job_result(processed=5)

    bulk_client.poll_until_complete.side_effect = poll

    run_bg(bulk_client, scan_store, minio_client, core_client)

    stored = scan_store.get("scan-1")
    assert stored.progress["Contact"].state == "Failed"
    assert "socket closed" in stored.progress["Contact"].error
    assert stored.progress["Account"].state == "JobComplete"
    assert stored.status == ScanStatus.FAILED  # any failure => FAILED


def test_background_upload_exception_marks_object_failed(
    scan_store, bulk_client, minio_client, core_client
):
    make_scan(scan_store, progress={"Contact": ObjectProgress(sf_job_id="j1", state="UploadComplete")})
    bulk_client.iter_results.side_effect = lambda *a, **k: iter([[{"Id": "001"}]])
    minio_client.upload_records.side_effect = ConnectionError("minio down")

    run_bg(bulk_client, scan_store, minio_client, core_client)

    stored = scan_store.get("scan-1")
    assert stored.progress["Contact"].state == "Failed"
    assert "minio down" in stored.progress["Contact"].error
    assert stored.status == ScanStatus.FAILED
    bulk_client.delete_job.assert_not_called()  # don't delete SF results we failed to store


def test_background_deduplicates_records_within_a_page(
    scan_store, bulk_client, minio_client, core_client
):
    make_scan(scan_store, progress={"Contact": ObjectProgress(sf_job_id="j1", state="UploadComplete")})
    bulk_client.iter_results.side_effect = lambda *a, **k: iter([
        [{"Id": "001", "Name": "A"}, {"Id": "001", "Name": "A"}, {"Id": "002", "Name": "B"}],
    ])

    run_bg(bulk_client, scan_store, minio_client, core_client)

    uploaded = minio_client.upload_records.call_args.kwargs["records"]
    assert sorted(r["Id"] for r in uploaded) == ["001", "002"]


def test_background_stops_when_scan_already_cancelled(scan_store, bulk_client, minio_client, core_client):
    make_scan(
        scan_store,
        status=ScanStatus.CANCELLED,
        progress={"Contact": ObjectProgress(sf_job_id="j1", state="UploadComplete")},
    )

    run_bg(bulk_client, scan_store, minio_client, core_client)

    bulk_client.poll_until_complete.assert_not_called()
    assert scan_store.get("scan-1").status == ScanStatus.CANCELLED


def test_background_aborted_job_marks_scan_cancelled(scan_store, bulk_client, minio_client, core_client):
    """If Salesforce reports Aborted (e.g. cancelled via SF UI), the scan
    ends CANCELLED rather than FAILED."""
    make_scan(scan_store, progress={"Contact": ObjectProgress(sf_job_id="j1", state="UploadComplete")})
    bulk_client.poll_until_complete.return_value = job_result(state="Aborted", processed=0)

    run_bg(bulk_client, scan_store, minio_client, core_client)

    assert scan_store.get("scan-1").status == ScanStatus.CANCELLED
    bulk_client.iter_results.assert_not_called()


@pytest.mark.xfail(
    reason="Core Service status reporting is commented out in run_scan_background "
           "(dev has no Core Service). Remove xfail once it's re-enabled.",
    strict=False,
)
def test_background_reports_final_status_to_core_service(
    scan_store, bulk_client, minio_client, core_client
):
    make_scan(scan_store, progress={"Contact": ObjectProgress(sf_job_id="j1", state="UploadComplete")})
    bulk_client.poll_until_complete.return_value = job_result(processed=42)

    run_bg(bulk_client, scan_store, minio_client, core_client)

    core_client.report_scan_status.assert_called_once()
    args = core_client.report_scan_status.call_args.args
    assert args[0] == "scan-1"
    assert args[2] == 42  # total records


# ===========================================================================
# CONCURRENCY: cancel scan vs. background polling
# ===========================================================================

def test_cancel_during_background_poll_wins_over_stale_write(
    scan_store, bulk_client, minio_client, core_client
):
    """
    Race: run_scan_background is mid-poll (blocked in poll_until_complete)
    when a cancellation lands. The background task's post-poll write must
    respect the cancellation instead of clobbering it back to Failed/Complete.
    """
    make_scan(
        scan_store,
        progress={"Contact": ObjectProgress(sf_job_id="job-contact", state="InProgress")},
    )

    poll_started = threading.Event()
    release_poll = threading.Event()
    thread_exception = {}

    def slow_poll(job_id, max_timeout_seconds):
        poll_started.set()
        release_poll.wait(timeout=5)
        return job_result(processed=10)

    bulk_client.poll_until_complete.side_effect = slow_poll
    bulk_client.get_job_info.return_value = {"state": "Aborted"}

    def run_and_capture():
        try:
            run_bg(bulk_client, scan_store, minio_client, core_client)
        except Exception as e:  # surfaced in the assertion message below
            thread_exception["error"] = e

    bg_thread = threading.Thread(target=run_and_capture)
    bg_thread.start()

    started = poll_started.wait(timeout=5)
    assert started, f"background poll never started. Exception: {thread_exception.get('error')}"

    # Cancel while the background task is "stuck" inside poll_until_complete
    with scan_store.lock("scan-1"):
        stored = scan_store._scans["scan-1"]
        stored.progress["Contact"].state = "Aborted"
        stored.status = ScanStatus.CANCELLED
        scan_store._scans["scan-1"] = stored

    release_poll.set()
    bg_thread.join(timeout=5)

    assert "error" not in thread_exception, thread_exception.get("error")
    final = scan_store.get("scan-1")
    assert final.status == ScanStatus.CANCELLED, (
        "background task overwrote a cancellation that happened mid-poll"
    )
    assert final.progress["Contact"].state == "Aborted", (
        "background task's post-poll write clobbered the cancelled job's state"
    )
    minio_client.upload_records.assert_not_called()


def test_cancel_endpoint_during_background_poll(
    client, scan_store, bulk_client, minio_client, core_client
):
    """Same race, but cancellation goes through the real HTTP endpoint."""
    make_scan(
        scan_store,
        progress={"Contact": ObjectProgress(sf_job_id="job-contact", state="InProgress")},
    )

    poll_started = threading.Event()
    release_poll = threading.Event()

    def slow_poll(job_id, max_timeout_seconds):
        poll_started.set()
        release_poll.wait(timeout=5)
        return job_result(processed=10)

    bulk_client.poll_until_complete.side_effect = slow_poll
    bulk_client.get_job_info.return_value = {"state": "Aborted"}

    bg_thread = threading.Thread(
        target=run_bg, args=(bulk_client, scan_store, minio_client, core_client)
    )
    bg_thread.start()
    assert poll_started.wait(timeout=5)

    resp = client.post("/api/scan/scan-1/cancel")
    assert resp.json()["success"] is True

    release_poll.set()
    bg_thread.join(timeout=5)

    final = scan_store.get("scan-1")
    assert final.status == ScanStatus.CANCELLED
    assert final.progress["Contact"].state == "Aborted"
    minio_client.upload_records.assert_not_called()


# ===========================================================================
# HMAC AUTH (real verify_hmac, no dependency override)
# ===========================================================================

HMAC_CORE_KEY = "core-key-" + "x" * 32
HMAC_ENGINEER_KEY = "engineer-key-" + "y" * 32


@pytest.fixture
def hmac_client(scan_store):
    app = FastAPI()

    @app.middleware("http")
    async def capture_raw_body(request: Request, call_next):
        request.state.raw_body = await request.body()
        return await call_next(request)

    app.include_router(scan.router)
    app.state.scan_store = scan_store
    app.state.settings = MagicMock(
        HMAC_ENABLED=True,
        HMAC_SECRET_KEY_CORE=HMAC_CORE_KEY,
        HMAC_SECRET_KEY_ENGINEER=HMAC_ENGINEER_KEY,
        HMAC_SIGNATURE_MAX_AGE=300,
    )
    return TestClient(app)


def sign(body: str = "", key: str = HMAC_CORE_KEY, key_id: str = "core", timestamp: float | None = None):
    ts = str(timestamp if timestamp is not None else time.time())
    sig = hmac.new(key.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return {"X-Signature": sig, "X-Timestamp": ts, "X-Key-Id": key_id}


def test_hmac_missing_headers_rejected(hmac_client):
    assert hmac_client.get("/api/scan/list").status_code == 422


def test_hmac_valid_core_signature_accepted(hmac_client):
    resp = hmac_client.get("/api/scan/list", headers=sign())
    assert resp.status_code == 200


def test_hmac_valid_engineer_signature_accepted(hmac_client):
    resp = hmac_client.get(
        "/api/scan/list", headers=sign(key=HMAC_ENGINEER_KEY, key_id="engineer")
    )
    assert resp.status_code == 200


def test_hmac_wrong_key_rejected(hmac_client):
    resp = hmac_client.get("/api/scan/list", headers=sign(key="not-the-real-key"))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid signature"


def test_hmac_unknown_key_id_rejected(hmac_client):
    resp = hmac_client.get("/api/scan/list", headers=sign(key_id="intruder"))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Unknown key id"


def test_hmac_expired_signature_rejected(hmac_client):
    resp = hmac_client.get("/api/scan/list", headers=sign(timestamp=time.time() - 3600))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Signature has expired"


def test_hmac_signature_bound_to_body(hmac_client):
    """A signature for one body must not validate a different body."""
    signed_for = '{"scan_id": "a", "org_id": "o", "objects": ["Contact"]}'
    sent = '{"scan_id": "b", "org_id": "o", "objects": ["Contact"]}'

    resp = hmac_client.post(
        "/api/scan/start",
        content=sent,
        headers={**sign(body=signed_for), "Content-Type": "application/json"},
    )

    assert resp.status_code == 401


def test_hmac_protects_every_scan_endpoint(hmac_client):
    """The router-level dependency should guard all routes, not just some."""
    for method, path in [
        ("get", "/api/scan/list"),
        ("get", "/api/scan/x/status"),
        ("post", "/api/scan/x/cancel"),
        ("post", "/api/scan/x/resume"),
        ("delete", "/api/scan/x/remove"),
    ]:
        resp = getattr(hmac_client, method)(path, headers={
            "X-Signature": "bad", "X-Timestamp": str(time.time()), "X-Key-Id": "core",
        })
        assert resp.status_code == 401, f"{method.upper()} {path} not protected"