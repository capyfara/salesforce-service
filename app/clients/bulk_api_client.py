# Salesforce Bulk API 2.0

import csv
import io
import time
import logging
import requests
from dataclasses import dataclass
from typing import Iterator, Optional

from clients.retry import RetryPolicy, with_retry

logger = logging.getLogger(__name__)

API_VERSION = "v59.0"
JOBS_BASE = f"/services/data/{API_VERSION}/jobs/query"
LIMITS_PATH = f"/services/data/{API_VERSION}/limits"

@dataclass
class BulkJobResult:
    job_id: str
    state: str                    # JobComplete | Failed | Aborted
    records_processed: int
    records_failed: int
    error_message: Optional[str]

import time
import requests
from clients.retry import RetryPolicy, with_retry

class SalesforceBulkAPIClient:
    POLL_INTERVALS = [5, 5, 5, 5, 5, 15, 15, 15, 60, 60, 120]

    def __init__(self, token_manager, timeout: int = 30, retry_policy: RetryPolicy | None = None):
        self._tokens = token_manager
        self._timeout = timeout
        self._retry = retry_policy or RetryPolicy()

    def _headers(self) -> dict:
        token, _ = self._tokens.get_token()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _base_url(self) -> str:
        _, instance_url = self._tokens.get_token()
        return instance_url

    def _request(self, method, url, *, idempotent=True, timeout=None, **kw):
        timeout = timeout or self._timeout

        def call():
            resp = requests.request(method, url, headers=self._headers(), timeout=timeout, **kw)
            if resp.status_code == 401:            # expired session: refresh once
                self._tokens.invalidate()
                resp = requests.request(method, url, headers=self._headers(), timeout=timeout, **kw)
            resp.raise_for_status()
            return resp

        return with_retry(call, self._retry, label=f"{method} {url.split('/jobs/')[-1]}",
                          idempotent=idempotent)

    def create_query_job(self, soql: str) -> str:
        url = f"{self._base_url()}{JOBS_BASE}"
        payload = {"operation": "query", "query": soql, "contentType": "CSV",
                   "columnDelimiter": "COMMA", "lineEnding": "LF"}
        return self._request("POST", url, json=payload, idempotent=False).json()["id"]

    def get_job_info(self, job_id: str) -> dict:
        return self._request("GET", f"{self._base_url()}{JOBS_BASE}/{job_id}").json()

    def abort_job(self, job_id: str) -> None:
        self._request("PATCH", f"{self._base_url()}{JOBS_BASE}/{job_id}", json={"state": "Aborted"})

    def poll_until_complete(self, job_id: str, max_timeout_seconds: int = 7200):
        deadline = time.monotonic() + max_timeout_seconds
        intervals = iter(self.POLL_INTERVALS)
        while True:
            data = self.get_job_info(job_id)
            if data["state"] in ("JobComplete", "Failed", "Aborted"):
                return BulkJobResult(job_id=job_id, state=data["state"],
                                     records_processed=data.get("numberRecordsProcessed", 0),
                                     records_failed=data.get("numberRecordsFailed", 0),
                                     error_message=data.get("errorMessage"))
            if time.monotonic() > deadline:
                raise TimeoutError(f"Job {job_id} exceeded {max_timeout_seconds}s")
            time.sleep(next(intervals, 120))

    def iter_results(self, job_id: str, page_size: int = 50000):
        locator = None
        while True:
            url = f"{self._base_url()}{JOBS_BASE}/{job_id}/results?maxRecords={page_size}"
            if locator:
                url += f"&locator={locator}"
            resp = self._request("GET", url, timeout=self._timeout * 6)
            yield list(csv.DictReader(io.StringIO(resp.text)))
            locator = resp.headers.get("Sforce-Locator")
            if not locator or locator == "null":
                break

    def delete_job(self, job_id: str) -> None:
        try:
            self._request("DELETE", f"{self._base_url()}{JOBS_BASE}/{job_id}")
        except Exception as e:
            logger.warning(f"Failed to delete job {job_id}: {e}")