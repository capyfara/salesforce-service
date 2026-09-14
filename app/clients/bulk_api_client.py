# Salesforce Bulk API 2.0

import csv
import io
import time
import logging
import requests
from dataclasses import dataclass
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

API_VERSION = "v59.0"
JOBS_BASE = f"/services/data/{API_VERSION}/jobs/query"


@dataclass
class BulkJobResult:
    job_id: str
    state: str                    # JobComplete | Failed | Aborted
    records_processed: int
    records_failed: int
    error_message: Optional[str]


class SalesforceBulkAPIClient:
    """
    Wrapper for Salesforce Bulk API 2.0 (query operations only).
    Handles job creation, polling, paginated result download, and cleanup.
    """

    POLL_INTERVALS = [5, 5, 5, 5, 5, 15, 15, 15, 60, 60, 120]  # seconds

    def __init__(self, token_manager, timeout: int = 30):
        self._tokens = token_manager
        self._timeout = timeout

    def _headers(self) -> dict:
        token, _ = self._tokens.get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _base_url(self) -> str:
        _, instance_url = self._tokens.get_token()
        return instance_url

    # ------------------------------------------------------------------
    # Job lifecycle
    # ------------------------------------------------------------------

    def create_query_job(self, soql: str) -> str:
        """Creates a Bulk API 2.0 query job. Returns job_id."""
        url = f"{self._base_url()}{JOBS_BASE}"
        payload = {
            "operation": "query",
            "query": soql,
            "contentType": "CSV",
            "columnDelimiter": "COMMA",
            "lineEnding": "LF",
        }
        resp = requests.post(url, json=payload, headers=self._headers(), timeout=self._timeout)
        resp.raise_for_status()
        job_id = resp.json()["id"]

        logger.info(f"Created Bulk API query job: {job_id}")
        return job_id

    def poll_until_complete(self, job_id: str, max_timeout_seconds: int) -> BulkJobResult:
        """Blocks until the job reaches a terminal state or times out. Uses adaptive polling."""
        url = f"{self._base_url()}{JOBS_BASE}/{job_id}"
        intervals = iter(self.POLL_INTERVALS)
        start = time.monotonic()

        while True:
            if time.monotonic() - start > max_timeout_seconds:
                # Give up waiting — abort it and report Failed
                try: 
                    self.abort_job(job_id)
                except Exception as e:
                    pass
                return BulkJobResult(
                    job_id=job_id, 
                    state="Failed",
                    records_processed=0, 
                    records_failed=0,
                    error_message=f"Timed out after {max_timeout_seconds}s"
                )
            
            resp = requests.get(url, headers=self._headers(), timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()

            if data["state"] in ("JobComplete", "Failed", "Aborted"):
                return BulkJobResult(
                    job_id=job_id,
                    state=data["state"],
                    records_processed=data.get("numberRecordsProcessed", 0),
                    records_failed=data.get("numberRecordsFailed", 0),
                    error_message=data.get("errorMessage"),
                )

            delay = next(intervals, 120)
            logger.debug(f"Job {job_id} state={data['state']}, waiting {delay}s")
            time.sleep(delay)

    def iter_results(self, job_id: str, page_size: int = 50000) -> Iterator[list[dict]]:
        """
        Yields pages of records (each page is a list of dicts).
        Handles Sforce-Locator pagination transparently.
        """
        locator = None
        page_num = 0
        while True:
            url = f"{self._base_url()}{JOBS_BASE}/{job_id}/results?maxRecords={page_size}"
            if locator:
                url += f"&locator={locator}"

            resp = requests.get(url, headers=self._headers(), timeout=self._timeout * 6)
            resp.raise_for_status()

            # Parse CSV body
            text = resp.text
            reader = csv.DictReader(io.StringIO(text))
            records = list(reader)
            page_num += 1
            logger.info(f"Job {job_id} page {page_num}: {len(records)} records")

            yield records

            locator = resp.headers.get("Sforce-Locator")
            if not locator or locator == "null":
                break

    def delete_job(self, job_id: str) -> None:
        """Deletes a completed job from Salesforce (best-effort cleanup)."""
        url = f"{self._base_url()}{JOBS_BASE}/{job_id}"
        try:
            resp = requests.delete(url, headers=self._headers(), timeout=self._timeout)
            resp.raise_for_status()
            logger.info(f"Deleted Bulk API job: {job_id}")
        except Exception as e:
            logger.warning(f"Failed to delete job {job_id}: {e}")

    def abort_job(self, job_id: str) -> None:
        """Aborts a query job via PATCH request for JobComplete and InProgress states."""
        url = f"{self._base_url()}{JOBS_BASE}/{job_id}"
        resp = requests.patch(
            url,
            json = {"state": "Aborted"},
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        logger.info(f"Aborted Bulk API job: {job_id}")

    '''Get objects'''
    def get_objects(self):
        url = f"{self._base_url()}services/data/v67.0/objects/"
        resp = requests.get(url, headers=self._headers(), timeout=self._timeout)
        print("Status:", resp.status_code)
        print("Response:", resp.text)

        return resp.json()
    
    '''Using for debug'''
    def get_job_info(self, job_id: str) -> str:
        url = f"{self._base_url()}{JOBS_BASE}/{job_id}"
        resp = requests.get(url, headers=self._headers(), timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()
     