"""
Integration tests for POST /api/scan/{id}/cancel and POST /api/scan/{id}/resume.

These tests use a REAL ScanStore so the locking behavior 
is actually exercised, not mocked away. 
Only the Salesforce Bulk API client and MinIO client are mocked, 
since those are the external boundaries.

"""

import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def scan_store():
    return ScanStore()


@pytest.fixture
def bulk_client():
    """A MagicMock bulk client. Individual tests configure return_value /
    side_effect on the methods they care about."""
    client = MagicMock()
    client.create_query_job.return_value = "750fakejobid000001"
    client.get_job_info.return_value = {"state": "Aborted"}
    return client


@pytest.fixture
def minio_client():
    client = MagicMock()
    client.upload_records.return_value = "s3://fake-bucket/fake/path/page_001.parquet"
    return client


@pytest.fixture
def app(scan_store, bulk_client, minio_client):
    app = FastAPI()
    app.include_router(scan.router)
    app.state.scan_store = scan_store
    app.state.bulk_client = bulk_client
    app.state.minio_client = minio_client
    app.state.settings = MagicMock(SF_MAX_JOB_TIMEOUT_HOURS=2)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def make_scan(scan_store, scan_id="scan-1", org_id="org-1",
              status=ScanStatus.IN_PROGRESS,
              progress=None, last_modified_after=None):
    """Helper to seed a scan directly into the store, bypassing /scan/start."""
    scan = ScanState(scan_id=scan_id, org_id=org_id)
    scan.status = status
    scan.progress = progress or {}
    scan.updated_at = datetime.now(timezone.utc)
    if hasattr(scan, "last_modified_after"):
        scan.last_modified_after = last_modified_after
    scan_store.create(scan)
    return scan


# ===========================================================================
# CANCEL (scan) TESTS
# ===========================================================================

def test_cancel_scan_not_found(client):
    resp = client.post("/api/scan/does-not-exist/cancel")
    assert resp.status_code == 404


def test_cancel_aborts_in_progress_jobs(client, scan_store, bulk_client):
    """Jobs still running should be PATCHed to Aborted and confirmed via GET."""
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

    # abort_job should have been called for each in-progress job
    assert bulk_client.abort_job.call_count == 2
    bulk_client.abort_job.assert_any_call("job-contact")
    bulk_client.abort_job.assert_any_call("job-account")

    # Store reflects confirmed state, not an assumed one
    scan = scan_store.get("scan-1")
    assert scan.status == ScanStatus.CANCELLED
    assert scan.progress["Contact"].state == "Aborted"
    assert scan.progress["Account"].state == "Aborted"


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

    # abort_job must NOT have been called on the already-complete job
    called_ids = [c.args[0] for c in bulk_client.abort_job.call_args_list]
    assert "job-contact" not in called_ids

    scan = scan_store.get("scan-1")
    assert scan.progress["Contact"].state == "JobComplete"  # untouched
    assert scan.progress["Contact"].records_processed == 100  # untouched


def test_cancel_reports_failure_when_abort_not_confirmed(client, scan_store, bulk_client):
    """If Salesforce doesn't confirm Aborted, success must be False and the
    job's real (uncertain) state should be reflected, not assumed."""
    make_scan(
        scan_store,
        progress={
            "Contact": ObjectProgress(sf_job_id="job-contact", state="InProgress"),
        },
    )
    # abort_job "succeeds" (no exception) but SF reports it actually completed
    # in the interim, not Aborted.
    bulk_client.get_job_info.return_value = {"state": "JobComplete"}

    resp = client.post("/api/scan/scan-1/cancel")
    body = resp.json()

    assert body["success"] is False
    assert body["jobs_aborted"] == []
    assert body["jobs_failed_to_abort"] == ["job-contact"]

    scan = scan_store.get("scan-1")
    assert scan.progress["Contact"].state == "JobComplete"  # trusts SF, not intent


def test_cancel_handles_abort_call_exception(client, scan_store, bulk_client):
    """Network/API errors during abort should land in jobs_failed_to_abort,
    not crash the endpoint or silently claim success."""
    make_scan(
        scan_store,
        progress={
            "Contact": ObjectProgress(sf_job_id="job-contact", state="InProgress"),
        },
    )
    bulk_client.abort_job.side_effect = Exception("connection reset")

    resp = client.post("/api/scan/scan-1/cancel")
    body = resp.json()

    assert resp.status_code == 200
    assert body["success"] is False
    assert body["jobs_failed_to_abort"] == ["job-contact"]


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
    body = second.json()
    assert body["message"] == "Already cancelled"
    # No additional abort attempts on the second call
    assert bulk_client.abort_job.call_count == 1


def test_cancel_does_not_touch_already_failed_or_aborted_objects(client, scan_store, bulk_client):
    make_scan(
        scan_store,
        progress={
            "Contact": ObjectProgress(sf_job_id="job-contact", state="Failed"),
            "Account": ObjectProgress(sf_job_id="job-account", state="Aborted"),
        },
    )
    resp = client.post("/api/scan/scan-1/cancel")
    body = resp.json()

    assert body["jobs_aborted"] == []
    assert body["jobs_already_complete"] == []
    assert bulk_client.abort_job.call_count == 0


# ===========================================================================
# RESUME TESTS
# ===========================================================================

def test_resume_not_found(client):
    resp = client.post("/api/scan/does-not-exist/resume")
    assert resp.status_code == 404


def test_resume_rejects_non_failed_scan(client, scan_store):
    make_scan(scan_store, status=ScanStatus.COMPLETE,
              progress={"Contact": ObjectProgress(sf_job_id="job-1", state="JobComplete")})

    resp = client.post("/api/scan/scan-1/resume")
    assert resp.status_code == 400


def test_resume_rejects_when_nothing_incomplete(client, scan_store):
    """Edge case: status says FAILED but somehow every object is complete
    (shouldn't normally happen, but the endpoint should guard against it)."""
    make_scan(
        scan_store,
        status=ScanStatus.FAILED,
        progress={"Contact": ObjectProgress(sf_job_id="job-1", state="JobComplete")},
    )
    resp = client.post("/api/scan/scan-1/resume")
    assert resp.status_code == 400


def test_resume_recreates_jobs_only_for_incomplete_objects(client, scan_store, bulk_client, monkeypatch):
    """Core resume behavior: completed objects are left untouched; failed
    objects get brand-new Salesforce jobs."""
    monkeypatch.setattr(extraction_service, "build_soql", lambda obj, filt: f"SELECT Id FROM {obj}")

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

    # Configure what poll_until_complete should return for the resumed job
    poll_result = MagicMock()
    poll_result.state = "JobComplete"
    poll_result.records_processed = 20
    poll_result.records_failed = 0
    poll_result.error_message = None
    bulk_client.poll_until_complete.return_value = poll_result

    # iter_results is also called once JobComplete — give it something iterable
    bulk_client.iter_results.return_value = iter([])

    resp = client.post("/api/scan/scan-1/resume")
    body = resp.json()

    assert resp.status_code == 200
    assert body["success"] is True
    assert body["resumed_objects"] == ["Account"]

    scan = scan_store.get("scan-1")
    print(f"Scan from store after resume: {scan}")
    assert scan.status == ScanStatus.COMPLETE

    # Completed object is untouched — same job id, same records, same paths
    contact = scan.progress["Contact"]
    assert contact.sf_job_id == "job-contact-old"
    assert contact.state == "JobComplete"
    assert contact.records_processed == 50

    # Failed object got a fresh job and reset progress fields
    account = scan.progress["Account"]
    assert account.sf_job_id == "job-account-new"
    assert account.state == "JobComplete"
    assert account.records_processed == 20
    assert account.error is None

    bulk_client.create_query_job.assert_called_once_with("SELECT Id FROM Account")


def test_resume_reuses_original_filter(client, scan_store, bulk_client, monkeypatch):
    """The stored last_modified_after should be passed back into build_soql
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
        progress={
            "Opportunity": ObjectProgress(sf_job_id="job-old", state="Failed"),
        },
    )
    bulk_client.create_query_job.return_value = "job-new"

    client.post("/api/scan/scan-1/resume")

    assert captured["Opportunity"] == "2026-04-01T00:00:00Z"


# ===========================================================================
# CONCURRENCY: cancel scan vs. background polling
# ===========================================================================

def test_cancel_during_background_poll_wins_over_stale_write(scan_store, bulk_client, minio_client):
    """
    Simulates race where run_scan_background is mid-poll 
    (blocked in poll_until_complete) 
    when cancel_scan runs concurrently.

    The background task's post-poll write must respect the cancellation
    instead of clobbering it back to Failed/Complete.
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
        result = MagicMock()
        result.state = "JobComplete"
        result.records_processed = 10
        result.records_failed = 0
        result.error_message = None
        return result

    bulk_client.poll_until_complete.side_effect = slow_poll
    bulk_client.iter_results.return_value = iter([])
    bulk_client.get_job_info.return_value = {"state": "Aborted"}

    def run_and_capture():
        try:
            extraction_service.run_scan_background(bulk_client, scan_store, "scan-1", minio_client, 7200)
        except Exception as e:
            thread_exception["error"] = e
            import traceback
            traceback.print_exc()

    bg_thread = threading.Thread(target=run_and_capture)
    bg_thread.start()

    started = poll_started.wait(timeout=5)
    if not started:
        bg_thread.join(timeout=1)
        print("Thread exception:", thread_exception.get("error"))
    assert started, f"background poll never started. Exception: {thread_exception.get('error')}"


    # Cancel while the background task is "stuck" inside poll_until_complete
    with scan_store.lock("scan-1"):
        scan = scan_store._scans["scan-1"]
        scan.progress["Contact"].state = "Aborted"
        scan.status = ScanStatus.CANCELLED
        scan_store._scans["scan-1"] = scan

    # Now let the background poll "finish" and try to write JobComplete
    release_poll.set()
    bg_thread.join(timeout=5)

    final = scan_store.get("scan-1")
    assert final.status == ScanStatus.CANCELLED, (
        "background task overwrote a cancellation that happened mid-poll"
    )
    assert final.progress["Contact"].state == "Aborted", (
        "background task's post-poll write clobbered the cancelled job's state"
    )