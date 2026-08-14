# Salesforce Service — Technical Design & Deployment Guide

**Version:** 1.0  
**Date:** 2026-05-08  
**Applies To:** Salesforce Integration Service  
**Author:** Glynac Engineering  

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Salesforce Bulk API Deep Dive](#3-salesforce-bulk-api-deep-dive)
4. [Service Design](#4-service-design)
5. [API Endpoints](#5-api-endpoints)
6. [Environment Variables & Vault Secrets](#6-environment-variables--vault-secrets)
7. [Nomad HCL — Development](#7-nomad-hcl--development)
8. [Nomad HCL — Staging](#8-nomad-hcl--staging)
9. [Nomad HCL — Production](#9-nomad-hcl--production)
10. [Data Flow](#10-data-flow)
11. [Integration with Existing Stack](#11-integration-with-existing-stack)
12. [Deployment Checklist](#12-deployment-checklist)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Overview

### What This Service Does

The **Salesforce Service** extracts data from Salesforce CRM using the Salesforce Bulk API 2.0 and feeds it into the Glynac data pipeline. It mirrors the role that `master-service` plays for the wealth platform API — acting as the dedicated extraction engine for Salesforce.

### Why Bulk API

Salesforce's standard REST API is rate-limited to ~100 requests per day per object for query results. For a production CRM that may hold hundreds of thousands of Contacts, Accounts, Opportunities, and Activities, the REST API is not viable for a full data extraction. The **Salesforce Bulk API 2.0** (Query Jobs) is designed specifically for large-scale reads:

- Processes up to **100 million records** per job
- Asynchronous: you create a job, poll for completion, then download results as CSV chunks
- No per-record API call overhead
- Results are paginated in 50,000-record chunks (locators)

This async job-and-poll pattern is identical to the wealth platform batch API that the master service already handles, making it a natural fit for the same architectural approach.

### Relationship to Existing Services

```
[core-service]         <- Orchestrator (creates/tracks/monitors scan jobs)
         |
         | HTTP + HMAC (internal)
         v
[salesforce-service]   <- NEW: This service (Salesforce extractor)
         |
         | OAuth 2.0 (Connected App)
         v
[Salesforce Bulk API 2.0]            <- External data source
         |
         | (results)
         v
[MinIO]                              <- Storage
```

---

## 2. Architecture

### Component Diagram

```
+---------------------------+        +--------------------------------+
|  Core Service             |        |  Salesforce Service            |
|  (Orchestrator)           |        |  (Extraction Engine)           |
|                           |        |                                |
|  POST /scans  ----------->| HMAC   |  POST /api/scan/start          |
|                           +------->|                                |
|  GET  /scans/status <-----+        |  GET  /api/scan/{id}/status    |
|                           |        |  GET  /api/scan/list           |
|  PUT  /scans/{id}/cancel ->        |  POST /api/scan/{id}/cancel    |
|                           |        |  POST /api/maintenance/cleanup |
+---------------------------+        |  GET  /api/health              |
                                     |  GET  /api/key/verify          |
                                     +----------------+---------------+
                                                      |
                                              OAuth 2.0 (JWT Bearer)
                                                      |
                                     +----------------v---------------+
                                     |  Salesforce Bulk API 2.0       |
                                     |                                |
                                     |  POST /jobs/query              |
                                     |  GET  /jobs/query/{jobId}      |
                                     |  GET  /jobs/query/{jobId}/     |
                                     |        results?maxRecords=...  |
                                     |  DELETE /jobs/query/{jobId}    |
                                     +--------------------------------+
                                                      |
                                              (CSV result chunks)
                                                      |
                                                +-----v---+
                                                | MinIO   |
                                                | (raw    |
                                                |  files) |
                                                +---------+
```

### Technology Stack

| Layer | Technology | Reason |
|---|---|---|
| Service framework | Flask-RESTX | Consistent with master-service; synchronous OK for long-running batch jobs |
| Salesforce client | `simple-salesforce` + custom Bulk API 2.0 wrapper | `simple-salesforce` handles auth; raw requests needed for Bulk API 2.0 |
| Auth to Salesforce | OAuth 2.0 — JWT Bearer flow (Connected App) | Non-interactive, no user session required; suitable for service-to-service |
| Data serialization | CSV → Parquet (via pandas/pyarrow) | Raw Salesforce bulk output is CSV; normalized to Parquet for pipeline |
| Object storage | MinIO (S3-compatible) | Same pattern as master-service |
| Secrets | HashiCorp Vault via Nomad template | Consistent with rest of platform |
| Auth (internal) | HMAC (dual-key: core-key + engineer-key) | Consistent with CLAUDE.md security standards |

---

## 3. Salesforce Bulk API Deep Dive

### 3.1 Authentication — JWT Bearer Flow

The service must never rely on username/password or interactive OAuth. Use the **JWT Bearer Token flow** with a Salesforce Connected App:

**Step 1 — Generate JWT:**

```
Header: {"alg": "RS256"}
Payload:
  iss:   <Connected App Consumer Key>
  sub:   <Salesforce username of the integration user>
  aud:   https://login.salesforce.com  (prod)
         https://test.salesforce.com   (sandbox/staging)
  exp:   <current_time + 180 seconds>
```

Sign with the RSA private key registered to the Connected App.

**Step 2 — Exchange JWT for access token:**

```
POST https://login.salesforce.com/services/oauth2/token
Content-Type: application/x-www-form-urlencoded

grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer
&assertion=<signed_jwt>
```

**Response:**

```json
{
  "access_token": "00D...",
  "instance_url": "https://yourorg.my.salesforce.com",
  "token_type": "Bearer"
}
```

**Token lifetime:** Access tokens expire after ~2 hours. The service must re-authenticate automatically before expiry (implement a token refresh wrapper around the Salesforce client).

**Vault secret path for credentials:**

```
secrets/data/glynac/salesforce-service-prod
  SF_CONSUMER_KEY         = <Connected App Consumer Key>
  SF_PRIVATE_KEY_PEM      = <RSA private key, PEM format>
  SF_USERNAME             = integration@yourorg.com
  SF_LOGIN_URL            = https://login.salesforce.com
```

---

### 3.2 Salesforce Bulk API 2.0 — Query Job Lifecycle

The Bulk API 2.0 (Query) has a strict lifecycle. Every extraction follows these steps:

```
1. CREATE JOB      POST   /services/data/v59.0/jobs/query
2. POLL STATUS     GET    /services/data/v59.0/jobs/query/{jobId}
3. PAGINATE/GET    GET    /services/data/v59.0/jobs/query/{jobId}/results
                          ?maxRecords=50000&locator=<cursor>
4. CLOSE JOB       DELETE /services/data/v59.0/jobs/query/{jobId}  (optional cleanup)
```

#### Step 1 — Create Query Job

```http
POST /services/data/v59.0/jobs/query
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "operation": "query",
  "query": "SELECT Id, Name, Email, Phone, AccountId, CreatedDate, LastModifiedDate FROM Contact WHERE LastModifiedDate >= 2026-01-01T00:00:00Z",
  "contentType": "CSV",
  "columnDelimiter": "COMMA",
  "lineEnding": "LF"
}
```

**Response (202 Accepted):**

```json
{
  "id": "7500W000005YeBBQA0",
  "operation": "query",
  "object": "Contact",
  "createdById": "005...",
  "createdDate": "2026-05-08T10:00:00.000+0000",
  "systemModstamp": "2026-05-08T10:00:00.000+0000",
  "state": "UploadComplete",
  "concurrencyMode": "Parallel",
  "contentType": "CSV",
  "apiVersion": 59.0,
  "lineEnding": "LF",
  "columnDelimiter": "COMMA"
}
```

#### Step 2 — Poll Job Status

```http
GET /services/data/v59.0/jobs/query/{jobId}
Authorization: Bearer <access_token>
```

**Response:**

```json
{
  "id": "7500W000005YeBBQA0",
  "state": "JobComplete",
  "numberRecordsProcessed": 142500,
  "numberRecordsFailed": 0,
  "retries": 0,
  "totalProcessingTime": 12450,
  "apiActiveProcessingTime": 11200
}
```

**State machine:**

```
UploadComplete -> InProgress -> JobComplete  (success path)
                             -> Failed       (error path — check errorMessage field)
                             -> Aborted      (cancelled)
```

**Recommended polling strategy:**

| Elapsed time | Poll interval |
|---|---|
| 0 – 30s | every 5s |
| 30s – 5min | every 15s |
| 5min – 30min | every 60s |
| > 30min | every 120s |

This prevents hammering the Salesforce API for large jobs while staying responsive for small ones.

#### Step 3 — Paginate Results

Salesforce returns up to 50,000 records per page. Use the `Sforce-Locator` response header to page through:

**First page:**

```http
GET /services/data/v59.0/jobs/query/{jobId}/results?maxRecords=50000
Authorization: Bearer <access_token>
```

**Response headers:**

```
Sforce-Locator: MjAwMDA=     <- base64 cursor; "null" if last page
Sforce-NumberOfRecords: 50000
Content-Type: text/csv
```

**Response body:** Raw CSV with header row:

```csv
Id,Name,Email,Phone,AccountId,CreatedDate,LastModifiedDate
003...,John Smith,john@example.com,555-1234,001...,2026-01-15T08:00:00.000+0000,2026-04-30T14:22:00.000+0000
...
```

**Subsequent pages:**

```http
GET /services/data/v59.0/jobs/query/{jobId}/results?maxRecords=50000&locator=MjAwMDA=
```

When `Sforce-Locator: null` is returned, all pages have been consumed.

#### Step 4 — Cleanup

```http
DELETE /services/data/v59.0/jobs/query/{jobId}
Authorization: Bearer <access_token>
```

Salesforce retains job results for 7 days. Deleting explicitly is good practice but not mandatory.

---

### 3.3 Supported Salesforce Objects for Extraction

The following objects are planned for extraction in the first release:

| Object | SOQL query scope | Estimated volume | Notes |
|---|---|---|---|
| `Contact` | All fields relevant to Glynac | Medium | Core CRM entity |
| `Account` | All fields relevant to Glynac | Medium | Firm/org data |
| `Opportunity` | All fields | Large | Pipeline/revenue data |
| `Activity` (Task + Event) | Completed + open | Large | Interaction history |
| `Lead` | Active + converted | Medium | Pre-conversion data |
| `User` | Active users only | Small | For user mapping |
| `CampaignMember` | All | Medium | Marketing attribution |

Each object maps to a dedicated MinIO prefix (see Section 10.2).

---

### 3.4 Rate Limits and Quotas

| Limit | Value | Mitigation |
|---|---|---|
| Max concurrent Bulk API jobs | 100 | Service enforces `MAX_CONCURRENT_SCANS` |
| Max records per job | 100M | Partition by date range if needed |
| Results retention | 7 days | Cleanup job runs nightly |
| API requests per 24h | ~15,000 (varies by org) | Exponential backoff on 429 |
| OAuth token lifetime | ~2 hours | Auto-refresh before expiry |

---

## 4. Service Design

### 4.1 Directory Structure

```
salesforce-service/
├── app/
│   ├── __init__.py
│   ├── main.py                  # Flask app factory, startup validation
│   ├── config.py                # Settings class with Pydantic validation
│   ├── routes.py                # API route definitions
│   ├── auth/
│   │   ├── __init__.py
│   │   └── salesforce_auth.py   # JWT Bearer token manager
│   ├── clients/
│   │   ├── __init__.py
│   │   └── bulk_api_client.py   # Salesforce Bulk API 2.0 wrapper
│   ├── services/
│   │   ├── __init__.py
│   │   ├── extraction_service.py   # Orchestrates bulk job lifecycle
│   │   ├── polling_service.py      # Async polling loop
│   │   ├── normalization_service.py # CSV -> Parquet conversion
│   │   ├── deduplication_service.py # Remove duplicate records
│   │   └── maintenance_service.py  # Cleanup old scans
│   ├── storage/
│   │   ├── __init__.py
│   │   └── minio_client.py      # MinIO upload helper
│   └── models/
│       ├── __init__.py
│       ├── scan.py              # Scan state model
│       └── job.py               # Salesforce job model
├── tests/
│   ├── unit/
│   ├── integration/
│   └── conftest.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── CLAUDE.md
└── README.md
```

### 4.2 Startup Validation (Fail Fast)

The service validates all configuration at startup before binding to the network port. This is mandatory per CLAUDE.md standards.

```python
# app/main.py

import sys
import logging
from app.config import SalesforceServiceSettings, validate_settings

logger = logging.getLogger(__name__)

def create_app():
    errors = validate_settings()
    if errors:
        logger.error("=" * 80)
        logger.error("[ERROR] CONFIGURATION VALIDATION FAILED")
        logger.error("=" * 80)
        for field, details in errors.items():
            logger.error(f"\n[{field}]")
            logger.error(f"  Error: {details['error']}")
            logger.error(f"  Fix:   {details['fix']}")
        logger.error("=" * 80)
        logger.error("Fix the errors above and restart the service.")
        logger.error("=" * 80)
        sys.exit(1)

    app = Flask(__name__)
    # ... register blueprints
    return app
```

### 4.3 Token Manager

```python
# app/auth/salesforce_auth.py

import time
import jwt
import requests
from threading import Lock

class SalesforceTokenManager:
    """Manages OAuth 2.0 JWT Bearer tokens with automatic refresh."""

    def __init__(self, consumer_key, private_key_pem, username, login_url):
        self._consumer_key = consumer_key
        self._private_key = private_key_pem
        self._username = username
        self._login_url = login_url
        self._access_token = None
        self._instance_url = None
        self._expires_at = 0
        self._lock = Lock()

    def get_token(self) -> tuple[str, str]:
        """Returns (access_token, instance_url), refreshing if within 5 minutes of expiry."""
        with self._lock:
            if time.time() > (self._expires_at - 300):
                self._refresh()
        return self._access_token, self._instance_url

    def _refresh(self):
        now = int(time.time())
        claim = {
            "iss": self._consumer_key,
            "sub": self._username,
            "aud": self._login_url,
            "exp": now + 180,
        }
        signed_jwt = jwt.encode(claim, self._private_key, algorithm="RS256")

        resp = requests.post(
            f"{self._login_url}/services/oauth2/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": signed_jwt,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        self._access_token = data["access_token"]
        self._instance_url = data["instance_url"]
        self._expires_at = now + 7200  # tokens last ~2 hours
```

### 4.4 Bulk API Client

```python
# app/clients/bulk_api_client.py

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

    def poll_until_complete(self, job_id: str) -> BulkJobResult:
        """Blocks until the job reaches a terminal state. Uses adaptive polling."""
        url = f"{self._base_url()}{JOBS_BASE}/{job_id}"
        intervals = iter(self.POLL_INTERVALS)
        while True:
            resp = requests.get(url, headers=self._headers(), timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
            state = data["state"]

            if state in ("JobComplete", "Failed", "Aborted"):
                return BulkJobResult(
                    job_id=job_id,
                    state=state,
                    records_processed=data.get("numberRecordsProcessed", 0),
                    records_failed=data.get("numberRecordsFailed", 0),
                    error_message=data.get("errorMessage"),
                )

            delay = next(intervals, 120)
            logger.debug(f"Job {job_id} state={state}, waiting {delay}s")
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
```

---

## 5. API Endpoints

### 5.1 Endpoint Summary

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/health` | None | Health check — required by Nomad |
| GET | `/api/key/verify` | HMAC | Verify HMAC key permissions |
| POST | `/api/scan/start` | HMAC | Start a Salesforce extraction scan |
| GET | `/api/scan/list` | HMAC | List all scans with filters |
| GET | `/api/scan/{id}/status` | HMAC | Get scan status and progress |
| POST | `/api/scan/{id}/cancel` | HMAC | Cancel an in-progress scan |
| POST | `/api/scan/{id}/resume` | HMAC | Resume a failed scan |
| DELETE | `/api/scan/{id}/remove` | HMAC | Remove a scan record |
| GET | `/api/scan/statistics` | HMAC | Aggregate scan stats |
| POST | `/api/maintenance/cleanup` | HMAC (engineer-key) | Purge old scan records |
| GET | `/api/objects` | HMAC | List supported Salesforce objects |
| GET | `/api/batch/info` | HMAC | Salesforce org and API quota info |

---

### 5.2 POST /api/scan/start

Triggers a full extraction of one or more Salesforce objects using the Bulk API.

**Request:**

```json
{
  "scan_id": "scan-abc-123",
  "org_id": "glynac-org-001",
  "objects": ["Contact", "Account", "Opportunity"],
  "filters": {
    "last_modified_after": "2026-01-01T00:00:00Z"
  },
  "output_format": "parquet",
  "destination": {
    "minio_bucket": "salesforce-extracts"
  }
}
```

**Fields:**

| Field | Type | Required | Description |
|---|---|---|---|
| `scan_id` | string | Yes | Unique ID from core-service |
| `org_id` | string | Yes | Glynac organization identifier |
| `objects` | string[] | Yes | Salesforce objects to extract |
| `filters.last_modified_after` | ISO8601 | No | Incremental extraction filter |
| `output_format` | `parquet` / `json` | No | Default: `parquet` |
| `destination.minio_bucket` | string | No | Override default bucket |

**Response (202 Accepted):**

```json
{
  "success": true,
  "scan_id": "scan-abc-123",
  "status": "started",
  "jobs": {
    "Contact":     "7500W000005YeBBQA0",
    "Account":     "7500W000005YeBBQA1",
    "Opportunity": "7500W000005YeBBQA2"
  },
  "message": "3 Bulk API jobs created. Poll /api/scan/scan-abc-123/status for progress."
}
```

---

### 5.3 GET /api/scan/{id}/status

**Response (200 OK):**

```json
{
  "scan_id": "scan-abc-123",
  "org_id": "glynac-org-001",
  "status": "in_progress",
  "started_at": "2026-05-08T10:00:00Z",
  "updated_at": "2026-05-08T10:04:30Z",
  "progress": {
    "Contact": {
      "sf_job_id": "7500W000005YeBBQA0",
      "state": "JobComplete",
      "records_processed": 142500,
      "records_failed": 0,
      "pages_downloaded": 3,
      "minio_path": "s3://salesforce-extracts/glynac-org-001/scan-abc-123/contact/"
    },
    "Account": {
      "sf_job_id": "7500W000005YeBBQA1",
      "state": "InProgress",
      "records_processed": 0,
      "records_failed": 0,
      "pages_downloaded": 0,
      "minio_path": null
    },
    "Opportunity": {
      "sf_job_id": "7500W000005YeBBQA2",
      "state": "UploadComplete",
      "records_processed": 0,
      "records_failed": 0,
      "pages_downloaded": 0,
      "minio_path": null
    }
  },
  "totals": {
    "objects_total": 3,
    "objects_complete": 1,
    "objects_failed": 0,
    "records_extracted": 142500
  }
}
```

---

### 5.4 GET /api/health

No authentication required. Used by Nomad health checks.

**Response (200 OK):**

```json
{
  "status": "healthy",
  "service": "salesforce-service",
  "version": "1.0.0",
  "salesforce_connected": true,
  "minio_connected": true
}
```

If any dependency is unreachable, return 503 with `"status": "degraded"` and identify which component failed.

---

### 5.5 POST /api/scan/{id}/cancel

Aborts all in-progress Salesforce Bulk API jobs for this scan. Records partial results already written to MinIO.

**Response (200 OK):**

```json
{
  "success": true,
  "scan_id": "scan-abc-123",
  "status": "cancelled",
  "jobs_aborted": ["7500W000005YeBBQA1", "7500W000005YeBBQA2"],
  "jobs_already_complete": ["7500W000005YeBBQA0"],
  "message": "Scan cancelled. 1 object (Contact) completed before cancellation."
}
```

---

### 5.6 GET /api/objects

Returns the list of Salesforce objects this service supports, with their SOQL templates.

**Response (200 OK):**

```json
{
  "supported_objects": [
    {
      "name": "Contact",
      "label": "Contacts",
      "soql_template": "SELECT {fields} FROM Contact {where_clause} ORDER BY LastModifiedDate ASC",
      "default_fields": ["Id", "Name", "Email", "Phone", "AccountId", "OwnerId", "CreatedDate", "LastModifiedDate"],
      "supports_incremental": true
    },
    {
      "name": "Account",
      "label": "Accounts",
      "soql_template": "SELECT {fields} FROM Account {where_clause} ORDER BY LastModifiedDate ASC",
      "default_fields": ["Id", "Name", "Type", "Industry", "BillingCity", "BillingState", "OwnerId", "CreatedDate", "LastModifiedDate"],
      "supports_incremental": true
    },
    {
      "name": "Opportunity",
      "label": "Opportunities",
      "soql_template": "SELECT {fields} FROM Opportunity {where_clause} ORDER BY LastModifiedDate ASC",
      "default_fields": ["Id", "Name", "AccountId", "OwnerId", "StageName", "Amount", "CloseDate", "CreatedDate", "LastModifiedDate"],
      "supports_incremental": true
    }
  ]
}
```

---

### 5.7 GET /api/batch/info

Returns Salesforce org metadata and current API quota usage.

**Response (200 OK):**

```json
{
  "org": {
    "id": "00D...",
    "name": "Glynac Production",
    "instance_url": "https://glynac.my.salesforce.com",
    "api_version": "59.0",
    "sandbox": false
  },
  "api_limits": {
    "daily_api_requests": {
      "max": 15000,
      "remaining": 12340
    },
    "bulk_api_jobs": {
      "active": 2,
      "max_concurrent": 100
    }
  },
  "token_expires_in_seconds": 4823
}
```

---

## 6. Environment Variables & Vault Secrets

### 6.1 All Environment Variables

| Variable | Dev value | Staging value | Prod value | Description |
|---|---|---|---|---|
| `FLASK_ENV` | `development` | `production` | `production` | Flask environment |
| `FLASK_DEBUG` | `True` | `False` | `False` | Must be False in stage/prod |
| `SECRET_KEY` | any 32+ chars | 32+ char random | 32+ char random | Flask secret key |
| `PORT` | `5710` | `5711` | `5712` | Container listen port |
| `HOST` | `0.0.0.0` | `0.0.0.0` | `0.0.0.0` | Bind address |
| `ENVIRONMENT` | `dev` | `stage` | `prod` | Triggers validation rules |
| `LOG_LEVEL` | `DEBUG` | `INFO` | `WARNING` | Log verbosity |
| `LOG_FORMAT` | `text` | `json` | `json` | Structured logging in stage/prod |
| `LOKI_ENABLED` | `false` | `true` | `true` | Loki log shipping |
| `DB_HOST` | `localhost` | `<pg-host>` | `<pg-host>` | PostgreSQL host |
| `DB_PORT` | `5432` | `5432` | `5432` | PostgreSQL port |
| `DB_NAME` | `salesforce_dev` | `salesforce_stage` | `salesforce_prod` | Database name |
| `DB_USER` | `dev_user` | `sf_stage_user` | `sf_prod_user` | Database user |
| `DB_PASSWORD` | `dev_pass` | Vault-managed | Vault-managed | Database password |
| `DB_SCHEMA` | `public` | `salesforce` | `salesforce` | Schema name |
| `SF_CONSUMER_KEY` | test key | Connected App key | Connected App key | Salesforce OAuth consumer key |
| `SF_PRIVATE_KEY_PEM` | test PEM | Vault-managed | Vault-managed | RSA private key for JWT signing |
| `SF_USERNAME` | test@sandbox | stage@glynac.com | prod@glynac.com | Salesforce integration user |
| `SF_LOGIN_URL` | `https://test.salesforce.com` | `https://test.salesforce.com` | `https://login.salesforce.com` | Sandbox vs. production |
| `SF_API_VERSION` | `v59.0` | `v59.0` | `v59.0` | Salesforce API version |
| `SF_BULK_PAGE_SIZE` | `50000` | `50000` | `50000` | Records per result page |
| `SF_MAX_JOB_TIMEOUT_HOURS` | `2` | `6` | `6` | Abort stuck jobs after N hours |
| `MAX_CONCURRENT_SCANS` | `2` | `5` | `10` | Max parallel scans |
| `SCAN_TIMEOUT_HOURS` | `2` | `6` | `6` | Max scan duration |
| `CLEANUP_DAYS` | `7` | `30` | `30` | Retain scan records N days |
| `HMAC_ENABLED` | `false` | `true` | `true` | HMAC auth for internal calls |
| `HMAC_SECRET_KEY_CORE` | any | 32+ char random | 32+ char random | Core-service HMAC key |
| `HMAC_SECRET_KEY_ENGINEER` | any | 32+ char random | 32+ char random | Engineer HMAC key |
| `HMAC_SIGNATURE_MAX_AGE` | `300` | `300` | `300` | Signature TTL in seconds |
| `MINIO_ENABLED` | `false` | `true` | `true` | Enable MinIO storage |
| `MINIO_ENDPOINT` | `localhost:9000` | `minio.service.consul:9000` | `minio.service.consul:9000` | MinIO endpoint |
| `MINIO_ACCESS_KEY` | `minioadmin` | Vault-managed | Vault-managed | MinIO access key |
| `MINIO_SECRET_KEY` | `minioadmin` | Vault-managed | Vault-managed | MinIO secret key |
| `MINIO_SECURE` | `false` | `true` | `true` | TLS for MinIO |
| `MINIO_BUCKET` | `salesforce-dev` | `salesforce-stage` | `salesforce-prod` | Default bucket |
| `PII_MASKING_ENABLED` | `false` | `true` | `true` | Enable PII masking |
| `PII_SERVICE_URL` | `http://localhost:8100` | `http://pii-service.service.consul:8100` | `http://pii-service.service.consul:8100` | PII service URL |
| `PII_HMAC_KEY` | any | 32+ char random | 32+ char random | PII service HMAC key |
| `PII_SERVICE_ID` | `sf-service-dev` | `sf-service-stage` | `sf-service-prod` | PII service identifier |
| `ALLOWED_ORIGINS` | `*` | `https://staging.glynac.ai` | `https://app.glynac.ai` | CORS origins |
| `CORE_SERVICE_URL` | `http://localhost:5700` | `http://core-service-staging.service.consul:5705` | `http://core-service-prod.service.consul:5705` | Core service URL |

---

### 6.2 Vault Secret Paths

| Environment | Vault Path |
|---|---|
| Development | `secrets/data/glynac/salesforce-service-dev` |
| Staging | `secrets/data/glynac/salesforce-service-staging` |
| Production | `secrets/data/glynac/salesforce-service-prod` |

**To write secrets to Vault (staging example):**

```bash
vault kv put secrets/glynac/salesforce-service-staging \
  SECRET_KEY="$(openssl rand -hex 32)" \
  SF_CONSUMER_KEY="3MVG9..." \
  SF_PRIVATE_KEY_PEM="$(cat /path/to/private.pem | base64 -w0)" \
  SF_USERNAME="integration@glynac.ai.staging" \
  SF_LOGIN_URL="https://test.salesforce.com" \
  DB_PASSWORD="$(vault kv get -field=password secrets/glynac/postgres-staging)" \
  MINIO_ACCESS_KEY="..." \
  MINIO_SECRET_KEY="..." \
  HMAC_SECRET_KEY_CORE="$(openssl rand -hex 32)" \
  HMAC_SECRET_KEY_ENGINEER="$(openssl rand -hex 32)" \
  PII_HMAC_KEY="$(openssl rand -hex 32)"
```

> **Note:** `SF_PRIVATE_KEY_PEM` must be stored base64-encoded in Vault if it contains newlines. The service decodes it at startup.

---

## 7. Nomad HCL — Development

**File:** `nomad/dev/salesforce-service.hcl`

```hcl
job "Salesforce-Service-Dev-App" {
  datacenters = ["glynac-dc"]
  type        = "service"
  namespace   = "extraction-service"

  update {
    max_parallel     = 1
    health_check     = "task_states"
    min_healthy_time = "30s"
  }

  group "salesforce-dev-service" {
    count = 1

    network {
      port "http" {
        static       = 5710
        to           = 5710
        host_network = "private"
      }
    }

    service {
      name = "salesforce-service-dev"
      tags = ["apps", "logs.promtail"]
      port = "http"
      check {
        name     = "api-health"
        type     = "http"
        path     = "/api/health"
        interval = "30s"
        timeout  = "10s"
      }
    }

    constraint {
      attribute = "${attr.unique.hostname}"
      value     = "Worker-08"
    }

    task "salesforce-service" {
      driver = "docker"

      config {
        image       = "harbor-registry.service.consul:8085/glynac/salesforce-service:IMAGE_TAG_PLACEHOLDER"
        ports       = ["http"]
        dns_servers = ["172.17.0.1", "172.18.0.1", "8.8.8.8", "8.8.4.4", "1.1.1.1"]
      }

      vault {
        role = "glynac"
      }

      template {
        destination = "secrets/env"
        env         = true
        data        = <<EOF

APP_VERSION="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.APP_VERSION }}{{ end }}"
APP_TITLE="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.APP_TITLE }}{{ end }}"
FLASK_ENV="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.FLASK_ENV }}{{ end }}"
FLASK_DEBUG="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.FLASK_DEBUG }}{{ end }}"
SECRET_KEY="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.SECRET_KEY }}{{ end }}"
PORT="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.PORT }}{{ end }}"
HOST="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.HOST }}{{ end }}"
ENVIRONMENT="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.ENVIRONMENT }}{{ end }}"
LOG_LEVEL="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.LOG_LEVEL }}{{ end }}"
LOG_FORMAT="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.LOG_FORMAT }}{{ end }}"
LOKI_ENABLED="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.LOKI_ENABLED }}{{ end }}"

DB_HOST="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.DB_HOST }}{{ end }}"
DB_PORT="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.DB_PORT }}{{ end }}"
DB_NAME="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.DB_NAME }}{{ end }}"
DB_USER="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.DB_USER }}{{ end }}"
DB_PASSWORD="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.DB_PASSWORD }}{{ end }}"
DB_SCHEMA="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.DB_SCHEMA }}{{ end }}"

SF_CONSUMER_KEY="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.SF_CONSUMER_KEY }}{{ end }}"
SF_PRIVATE_KEY_PEM="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.SF_PRIVATE_KEY_PEM }}{{ end }}"
SF_USERNAME="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.SF_USERNAME }}{{ end }}"
SF_LOGIN_URL="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.SF_LOGIN_URL }}{{ end }}"
SF_API_VERSION="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.SF_API_VERSION }}{{ end }}"
SF_BULK_PAGE_SIZE="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.SF_BULK_PAGE_SIZE }}{{ end }}"
SF_MAX_JOB_TIMEOUT_HOURS="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.SF_MAX_JOB_TIMEOUT_HOURS }}{{ end }}"

MAX_CONCURRENT_SCANS="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.MAX_CONCURRENT_SCANS }}{{ end }}"
SCAN_TIMEOUT_HOURS="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.SCAN_TIMEOUT_HOURS }}{{ end }}"
CLEANUP_DAYS="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.CLEANUP_DAYS }}{{ end }}"

HMAC_ENABLED="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.HMAC_ENABLED }}{{ end }}"
HMAC_SECRET_KEY_CORE="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.HMAC_SECRET_KEY_CORE }}{{ end }}"
HMAC_SECRET_KEY_ENGINEER="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.HMAC_SECRET_KEY_ENGINEER }}{{ end }}"
HMAC_SIGNATURE_MAX_AGE="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.HMAC_SIGNATURE_MAX_AGE }}{{ end }}"

MINIO_ENABLED="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.MINIO_ENABLED }}{{ end }}"
MINIO_ENDPOINT="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.MINIO_ENDPOINT }}{{ end }}"
MINIO_ACCESS_KEY="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.MINIO_ACCESS_KEY }}{{ end }}"
MINIO_SECRET_KEY="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.MINIO_SECRET_KEY }}{{ end }}"
MINIO_SECURE="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.MINIO_SECURE }}{{ end }}"
MINIO_BUCKET="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.MINIO_BUCKET }}{{ end }}"

PII_MASKING_ENABLED="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.PII_MASKING_ENABLED }}{{ end }}"
PII_SERVICE_URL="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.PII_SERVICE_URL }}{{ end }}"
PII_HMAC_KEY="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.PII_HMAC_KEY }}{{ end }}"
PII_SERVICE_ID="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.PII_SERVICE_ID }}{{ end }}"

ALLOWED_ORIGINS="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.ALLOWED_ORIGINS }}{{ end }}"
CORE_SERVICE_URL="{{ with secret "secrets/data/glynac/salesforce-service-dev" }}{{ .Data.data.CORE_SERVICE_URL }}{{ end }}"

EOF
      }

      resources {
        cpu    = 512
        memory = 512
      }
    }
  }
}
```

---

## 8. Nomad HCL — Staging

**File:** `nomad/stage/salesforce-service.hcl`

```hcl
job "Salesforce-Service-Stage-App" {
  datacenters = ["glynac-dc"]
  type        = "service"
  namespace   = "extraction-service"

  update {
    max_parallel     = 1
    health_check     = "task_states"
    min_healthy_time = "30s"
  }

  group "salesforce-stage-service" {
    count = 1

    network {
      port "http" {
        static       = 5711
        to           = 5711
        host_network = "private"
      }
    }

    service {
      name = "salesforce-service-staging"
      tags = ["apps", "logs.promtail"]
      port = "http"
      check {
        name     = "api-health"
        type     = "http"
        path     = "/api/health"
        interval = "30s"
        timeout  = "10s"
      }
    }

    constraint {
      attribute = "${attr.unique.hostname}"
      value     = "Worker-08"
    }

    task "salesforce-service" {
      driver = "docker"

      config {
        image       = "harbor-registry.service.consul:8085/glynac/salesforce-service:IMAGE_TAG_PLACEHOLDER"
        ports       = ["http"]
        dns_servers = ["172.17.0.1", "172.18.0.1", "8.8.8.8", "8.8.4.4", "1.1.1.1"]
      }

      vault {
        role = "glynac"
      }

      template {
        destination = "secrets/env"
        env         = true
        data        = <<EOF

APP_VERSION="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.APP_VERSION }}{{ end }}"
APP_TITLE="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.APP_TITLE }}{{ end }}"
FLASK_ENV="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.FLASK_ENV }}{{ end }}"
FLASK_DEBUG="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.FLASK_DEBUG }}{{ end }}"
SECRET_KEY="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.SECRET_KEY }}{{ end }}"
PORT="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.PORT }}{{ end }}"
HOST="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.HOST }}{{ end }}"
ENVIRONMENT="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.ENVIRONMENT }}{{ end }}"
LOG_LEVEL="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.LOG_LEVEL }}{{ end }}"
LOG_FORMAT="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.LOG_FORMAT }}{{ end }}"
LOKI_ENABLED="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.LOKI_ENABLED }}{{ end }}"

DB_HOST="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.DB_HOST }}{{ end }}"
DB_PORT="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.DB_PORT }}{{ end }}"
DB_NAME="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.DB_NAME }}{{ end }}"
DB_USER="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.DB_USER }}{{ end }}"
DB_PASSWORD="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.DB_PASSWORD }}{{ end }}"
DB_SCHEMA="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.DB_SCHEMA }}{{ end }}"

SF_CONSUMER_KEY="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.SF_CONSUMER_KEY }}{{ end }}"
SF_PRIVATE_KEY_PEM="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.SF_PRIVATE_KEY_PEM }}{{ end }}"
SF_USERNAME="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.SF_USERNAME }}{{ end }}"
SF_LOGIN_URL="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.SF_LOGIN_URL }}{{ end }}"
SF_API_VERSION="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.SF_API_VERSION }}{{ end }}"
SF_BULK_PAGE_SIZE="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.SF_BULK_PAGE_SIZE }}{{ end }}"
SF_MAX_JOB_TIMEOUT_HOURS="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.SF_MAX_JOB_TIMEOUT_HOURS }}{{ end }}"

MAX_CONCURRENT_SCANS="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.MAX_CONCURRENT_SCANS }}{{ end }}"
SCAN_TIMEOUT_HOURS="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.SCAN_TIMEOUT_HOURS }}{{ end }}"
CLEANUP_DAYS="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.CLEANUP_DAYS }}{{ end }}"

HMAC_ENABLED="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.HMAC_ENABLED }}{{ end }}"
HMAC_SECRET_KEY_CORE="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.HMAC_SECRET_KEY_CORE }}{{ end }}"
HMAC_SECRET_KEY_ENGINEER="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.HMAC_SECRET_KEY_ENGINEER }}{{ end }}"
HMAC_SIGNATURE_MAX_AGE="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.HMAC_SIGNATURE_MAX_AGE }}{{ end }}"

MINIO_ENABLED="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.MINIO_ENABLED }}{{ end }}"
MINIO_ENDPOINT="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.MINIO_ENDPOINT }}{{ end }}"
MINIO_ACCESS_KEY="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.MINIO_ACCESS_KEY }}{{ end }}"
MINIO_SECRET_KEY="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.MINIO_SECRET_KEY }}{{ end }}"
MINIO_SECURE="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.MINIO_SECURE }}{{ end }}"
MINIO_BUCKET="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.MINIO_BUCKET }}{{ end }}"

PII_MASKING_ENABLED="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.PII_MASKING_ENABLED }}{{ end }}"
PII_SERVICE_URL="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.PII_SERVICE_URL }}{{ end }}"
PII_HMAC_KEY="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.PII_HMAC_KEY }}{{ end }}"
PII_SERVICE_ID="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.PII_SERVICE_ID }}{{ end }}"

ALLOWED_ORIGINS="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.ALLOWED_ORIGINS }}{{ end }}"
CORE_SERVICE_URL="{{ with secret "secrets/data/glynac/salesforce-service-staging" }}{{ .Data.data.CORE_SERVICE_URL }}{{ end }}"

EOF
      }

      resources {
        cpu    = 512
        memory = 512
      }
    }
  }
}
```

---

## 9. Nomad HCL — Production

**File:** `nomad/prod/salesforce-service.hcl`

```hcl
job "Salesforce-Service-Prod-App" {
  datacenters = ["glynac-dc"]
  type        = "service"
  namespace   = "extraction-service"

  update {
    max_parallel     = 1
    health_check     = "task_states"
    min_healthy_time = "30s"
  }

  group "salesforce-prod-service" {
    count = 1

    network {
      port "http" {
        static       = 5712
        to           = 5712
        host_network = "private"
      }
    }

    service {
      name = "salesforce-service-prod"
      tags = ["apps", "logs.promtail"]
      port = "http"
      check {
        name     = "api-health"
        type     = "http"
        path     = "/api/health"
        interval = "30s"
        timeout  = "10s"
      }
    }

    constraint {
      attribute = "${attr.unique.hostname}"
      value     = "Worker-08"
    }

    task "salesforce-service" {
      driver = "docker"

      config {
        image       = "harbor-registry.service.consul:8085/glynac/salesforce-service:IMAGE_TAG_PLACEHOLDER"
        ports       = ["http"]
        dns_servers = ["172.17.0.1", "172.18.0.1", "8.8.8.8", "8.8.4.4", "1.1.1.1"]
      }

      vault {
        role = "glynac"
      }

      template {
        destination = "secrets/env"
        env         = true
        data        = <<EOF

APP_VERSION="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.APP_VERSION }}{{ end }}"
APP_TITLE="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.APP_TITLE }}{{ end }}"
FLASK_ENV="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.FLASK_ENV }}{{ end }}"
FLASK_DEBUG="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.FLASK_DEBUG }}{{ end }}"
SECRET_KEY="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.SECRET_KEY }}{{ end }}"
PORT="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.PORT }}{{ end }}"
HOST="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.HOST }}{{ end }}"
ENVIRONMENT="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.ENVIRONMENT }}{{ end }}"
LOG_LEVEL="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.LOG_LEVEL }}{{ end }}"
LOG_FORMAT="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.LOG_FORMAT }}{{ end }}"
LOKI_ENABLED="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.LOKI_ENABLED }}{{ end }}"

DB_HOST="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.DB_HOST }}{{ end }}"
DB_PORT="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.DB_PORT }}{{ end }}"
DB_NAME="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.DB_NAME }}{{ end }}"
DB_USER="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.DB_USER }}{{ end }}"
DB_PASSWORD="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.DB_PASSWORD }}{{ end }}"
DB_SCHEMA="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.DB_SCHEMA }}{{ end }}"

SF_CONSUMER_KEY="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.SF_CONSUMER_KEY }}{{ end }}"
SF_PRIVATE_KEY_PEM="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.SF_PRIVATE_KEY_PEM }}{{ end }}"
SF_USERNAME="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.SF_USERNAME }}{{ end }}"
SF_LOGIN_URL="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.SF_LOGIN_URL }}{{ end }}"
SF_API_VERSION="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.SF_API_VERSION }}{{ end }}"
SF_BULK_PAGE_SIZE="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.SF_BULK_PAGE_SIZE }}{{ end }}"
SF_MAX_JOB_TIMEOUT_HOURS="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.SF_MAX_JOB_TIMEOUT_HOURS }}{{ end }}"

MAX_CONCURRENT_SCANS="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.MAX_CONCURRENT_SCANS }}{{ end }}"
SCAN_TIMEOUT_HOURS="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.SCAN_TIMEOUT_HOURS }}{{ end }}"
CLEANUP_DAYS="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.CLEANUP_DAYS }}{{ end }}"

HMAC_ENABLED="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.HMAC_ENABLED }}{{ end }}"
HMAC_SECRET_KEY_CORE="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.HMAC_SECRET_KEY_CORE }}{{ end }}"
HMAC_SECRET_KEY_ENGINEER="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.HMAC_SECRET_KEY_ENGINEER }}{{ end }}"
HMAC_SIGNATURE_MAX_AGE="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.HMAC_SIGNATURE_MAX_AGE }}{{ end }}"

MINIO_ENABLED="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.MINIO_ENABLED }}{{ end }}"
MINIO_ENDPOINT="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.MINIO_ENDPOINT }}{{ end }}"
MINIO_ACCESS_KEY="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.MINIO_ACCESS_KEY }}{{ end }}"
MINIO_SECRET_KEY="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.MINIO_SECRET_KEY }}{{ end }}"
MINIO_SECURE="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.MINIO_SECURE }}{{ end }}"
MINIO_BUCKET="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.MINIO_BUCKET }}{{ end }}"

PII_MASKING_ENABLED="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.PII_MASKING_ENABLED }}{{ end }}"
PII_SERVICE_URL="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.PII_SERVICE_URL }}{{ end }}"
PII_HMAC_KEY="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.PII_HMAC_KEY }}{{ end }}"
PII_SERVICE_ID="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.PII_SERVICE_ID }}{{ end }}"

ALLOWED_ORIGINS="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.ALLOWED_ORIGINS }}{{ end }}"
CORE_SERVICE_URL="{{ with secret "secrets/data/glynac/salesforce-service-prod" }}{{ .Data.data.CORE_SERVICE_URL }}{{ end }}"

EOF
      }

      resources {
        cpu    = 1024
        memory = 1024
      }
    }
  }
}
```

---

## 10. Data Flow

### 10.1 Full Extraction Sequence

```
1. Core Service receives extraction request from Glynac platform
   POST /scans  {org_id, scan_type: "salesforce", objects: [...]}

2. Core Service calls Salesforce Service (HMAC-authenticated)
   POST /api/scan/start  {scan_id, org_id, objects, filters}

3. Salesforce Service authenticates to Salesforce
   POST https://login.salesforce.com/services/oauth2/token
   <- {access_token, instance_url}

4. Salesforce Service creates one Bulk API job per object (parallelized)
   POST /services/data/v59.0/jobs/query  {query: "SELECT ... FROM Contact ..."}
   <- {id: "7500W...", state: "UploadComplete"}

5. Service polls each job for completion (adaptive intervals: 5s -> 15s -> 60s -> 120s)
   GET /services/data/v59.0/jobs/query/{jobId}
   <- {state: "JobComplete", numberRecordsProcessed: 142500}

6. Service downloads results in 50,000-record pages
   GET /services/data/v59.0/jobs/query/{jobId}/results?maxRecords=50000
   <- CSV data + Sforce-Locator header

7. Per page, in pipeline:
   a. Parse CSV -> list of dicts
   b. Apply PII masking (if enabled, via PII service)
   c. Deduplicate records by SF Id
   d. Convert to Parquet (pyarrow)
   e. Upload to MinIO:
      s3://salesforce-{env}/{org_id}/{scan_id}/{object}/page_{n}.parquet

8. After all pages downloaded, delete SF job (cleanup)
   DELETE /services/data/v59.0/jobs/query/{jobId}

9. Service reports scan complete to Core Service
   PATCH /scans/{scan_id}  {status: "completed", records: 142500}

10. Core Service marks scan done in its database
    Downstream consumers query MinIO
```

### 10.2 MinIO Object Layout

```
salesforce-prod/
  {org_id}/
    {scan_id}/
      contact/
        page_001.parquet      <- 50,000 records
        page_002.parquet
        page_003.parquet
        _metadata.json        <- {object, scan_id, total_records, pages, soql, created_at}
      account/
        page_001.parquet
        _metadata.json
      opportunity/
        page_001.parquet
        page_002.parquet
        _metadata.json
```

### 10.3 Incremental Extraction

For subsequent scans, use the `last_modified_after` filter to extract only changed records:

```sql
SELECT Id, Name, Email, ... FROM Contact
WHERE LastModifiedDate >= 2026-04-01T00:00:00Z
ORDER BY LastModifiedDate ASC
```

The Core Service tracks `last_scan_at` per org per object type and passes it as `filters.last_modified_after` in subsequent scan requests. This reduces extraction volume by 90%+ after the initial full load.

---

## 11. Integration with Existing Stack

### 11.1 Port Allocation

| Service | Dev port | Stage port | Prod port | Container port |
|---|---|---|---|---|
| Core Service | 5701 (dev) | 5705 (stage) | 5705 (prod) | 5700 |
| Master Service | (dev) | 5708 (stage) | 5708 (prod) | 5707 |
| Salesforce Service | **5710** | **5711** | **5712** | **5712** |

> Ports 5710-5712 are proposed as unused. Verify against current Nomad job list before deploying.

### 11.2 Consul Service Discovery

The service registers itself in Consul as:
- Dev: `salesforce-service-dev`
- Stage: `salesforce-service-staging`
- Prod: `salesforce-service-prod`

Core Service uses the Consul name for service-to-service calls. The `CORE_SERVICE_URL` env var on the Salesforce service points back to the Core service for status callbacks.

### 11.3 Vault Policy

Add the following path to the `glynac` Vault role's policy:

```hcl
# Existing paths (already in policy)
path "secrets/data/glynac/master-service-*" {
  capabilities = ["read"]
}
path "secrets/glynac/core-*" {
  capabilities = ["read"]
}

# New path to add
path "secrets/data/glynac/salesforce-service-*" {
  capabilities = ["read"]
}
```

### 11.4 Harbor Image Repository

Docker images follow the existing pattern:

```
harbor-registry.service.consul:8085/glynac/salesforce-service:<tag>
```

Create the project in Harbor before pushing:

```bash
# Harbor project name: glynac (already exists)
# Repository name: salesforce-service (new)
```

CI/CD pipeline tags: `latest`, `git-{short-sha}`, `v{semver}`.

---

## 12. Deployment Checklist

### 12.1 Pre-requisites (One Time)

- [ x ] Salesforce Connected App created with OAuth 2.0 JWT Bearer enabled
- [ x ] RSA key pair generated (`openssl genrsa -out private.pem 2048 && openssl rsa -in private.pem -pubout -out public.pem`)
- [ x ] Public key uploaded to Connected App in Salesforce Setup
- [ x ] Integration user created in Salesforce with minimal required permissions:
  - API Enabled 
  - View All Data (or per-object Read access for: Contact, Account, Opportunity, Activity, Lead, User, CampaignMember)
  - Bulk API Hard Delete (if cleanup jobs needed)
- [ x ] Connected App approved for the integration user profile
- [ ] Vault policy updated with new secret path (Section 11.3)
- [ ] Harbor repository created for the image
- [ ] MinIO buckets created: `salesforce-dev`, `salesforce-stage`, `salesforce-prod`

### 12.2 Before Deploying to Staging

**Vault Secrets**
- [ ] All secrets written to `secrets/data/glynac/salesforce-service-staging`
- [ ] `SF_LOGIN_URL` = `https://test.salesforce.com` (sandbox)
- [ ] `SECRET_KEY` is 32+ characters, not a placeholder
- [ ] `HMAC_SECRET_KEY_CORE` matches the key configured in Core Service staging
- [ ] `HMAC_SECRET_KEY_ENGINEER` is 32+ characters
- [ ] `PII_HMAC_KEY` matches the PII service staging key
- [ ] No `REPLACE_WITH_*` placeholder values

**Config Validation**
- [ ] `FLASK_DEBUG=false`
- [ ] `ENVIRONMENT=stage`
- [ ] `HMAC_ENABLED=true`
- [ ] `ALLOWED_ORIGINS` is HTTPS only (no wildcard)
- [ ] `MINIO_SECURE=true`
- [ ] `LOG_LEVEL=INFO`
- [ ] `PII_MASKING_ENABLED=true`

**Testing**
- [ ] Unit tests pass: `pytest tests/unit/ -v`
- [ ] Integration tests pass: `pytest tests/integration/ -v`
- [ ] Config validation tests pass: `pytest tests/unit/test_config.py -v`
- [ ] Health endpoint responds: `curl http://localhost:5711/api/health`
- [ ] Salesforce connection verified: `curl http://localhost:5711/api/batch/info` (with HMAC headers)
- [ ] Test scan completes end-to-end against Salesforce sandbox

**Nomad**
- [ ] HCL file validated: `nomad job validate nomad/stage/salesforce-service.hcl`
- [ ] `IMAGE_TAG_PLACEHOLDER` replaced with actual image tag
- [ ] Port 5711 not in use by another service
- [ ] Worker-08 has capacity for 512 CPU + 512 MB

### 12.3 Before Deploying to Production

**All Staging Checks Above, Plus:**

**Production-Specific**
- [ ] `SF_LOGIN_URL` = `https://login.salesforce.com` (production org, not sandbox)
- [ ] `FLASK_DEBUG=false` confirmed
- [ ] `LOG_LEVEL=WARNING`
- [ ] `MINIO_BUCKET=salesforce-prod`
- [ ] Rollback plan documented: previous image tag noted, rollback command ready
- [ ] Monitoring/alerting configured for the service in Grafana/Loki
- [ ] Load test passed: simulate 3 concurrent 100k-record scans
- [ ] Database backup confirmed for the salesforce PostgreSQL DB
- [ ] On-call notified of deployment window

**Deploy Command:**

```bash
nomad job run nomad/prod/salesforce-service.hcl
```

**Verify:**

```bash
nomad job status Salesforce-Service-Prod-App
nomad alloc status <alloc-id>
nomad alloc logs <alloc-id>
```

**Rollback (if needed):**

```bash
nomad job revert Salesforce-Service-Prod-App <previous-version>
```

---

## 13. Troubleshooting

### 13.1 Common Errors

#### `INVALID_SESSION_ID` from Salesforce

The access token has expired. The `SalesforceTokenManager` should prevent this by refreshing 5 minutes before expiry. If it occurs:

1. Check that `SF_LOGIN_URL` is correct for the environment (sandbox vs. prod).
2. Check that the RSA private key in Vault matches the public key on the Connected App.
3. Verify the integration user's session settings: Salesforce Setup > Session Settings > ensure "Lock sessions to the IP address from which they originated" is OFF for API users.

#### `REQUEST_LIMIT_EXCEEDED`

Daily API limit hit. Check `GET /api/batch/info` for remaining quota. Mitigation:
- Space out scans across the day
- Reduce concurrent jobs (`MAX_CONCURRENT_SCANS`)
- Use incremental extraction to minimize records per scan

#### Job stuck in `InProgress` for > 2 hours

Check `SF_MAX_JOB_TIMEOUT_HOURS` — the service should abort and mark it failed. If not:

```bash
# Manually abort via Salesforce API
curl -X DELETE \
  -H "Authorization: Bearer <token>" \
  https://yourorg.my.salesforce.com/services/data/v59.0/jobs/query/<jobId>
```

Then trigger a resume: `POST /api/scan/{id}/resume`

#### MinIO upload failing

```
ConnectionError: Failed to connect to minio.service.consul:9000
```

1. Check `MINIO_ENABLED` and `MINIO_ENDPOINT` in Vault.
2. Verify MinIO is healthy: `curl http://minio.service.consul:9000/minio/health/live`
3. Check that the bucket `salesforce-{env}` exists.

#### HMAC signature rejected by Core Service

Ensure `HMAC_SECRET_KEY_CORE` in this service matches `HMAC_SECRET_KEY_CORE` in the Core Service for the same environment. They must be identical — they are the shared secret for inter-service authentication.

### 13.2 Useful Nomad Commands

```bash
# Check job status
nomad job status Salesforce-Service-Prod-App

# Tail live logs
nomad alloc logs -f <alloc-id>

# Check resource usage
nomad alloc status <alloc-id>

# Restart the service (creates new allocation)
nomad job stop Salesforce-Service-Prod-App
nomad job run nomad/prod/salesforce-service.hcl

# List running allocations
nomad job allocs Salesforce-Service-Prod-App
```

### 13.3 Useful Vault Commands

```bash
# Read current secrets (to verify, never log output)
vault kv get secrets/data/glynac/salesforce-service-prod

# Update a single secret
vault kv patch secrets/data/glynac/salesforce-service-prod \
  HMAC_SECRET_KEY_CORE="<new-key>"

# After updating secrets, restart the Nomad job so templates re-render
nomad job stop Salesforce-Service-Prod-App
nomad job run nomad/prod/salesforce-service.hcl
```

---

*Document maintained by Glynac Engineering. For questions, raise a ticket in the Salesforce Service project.*
