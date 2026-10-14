# Salesforce Service

Extraction service that pulls CRM data out of Salesforce using the **Bulk API 2.0** and feeds it into the Glynac data pipeline. It is the Salesforce counterpart to `master-service`: **core-service** requests a scan over HMAC-signed HTTP, and this service creates the Bulk API jobs, downloads the results, stores them as Parquet in MinIO, loads them into ClickHouse, and reports the outcome back.

```
core-service ──HMAC──▶ salesforce-service ──JWT OAuth──▶ Salesforce Bulk API 2.0
                          │   │   │
                          │   │   └──▶ PostgreSQL   (scan state, advisory locks)
                          │   └──────▶ MinIO        (Parquet pages)
                          └──────────▶ ClickHouse   (typed tables loaded from MinIO)
```

For design rationale and Nomad/Vault deployment HCL, see [`salesforce-service-technical-design.md`](./salesforce-service-technical-design.md).

---

## Features

- **Bulk API 2.0 extraction**: one asynchronous query job per Salesforce object, with adaptive polling and `Sforce-Locator` pagination (50,000 records per page by default).
- **JWT Bearer authentication** to Salesforce, with automatic token refresh. No usernames, passwords or interactive login.
- **Full and incremental scans**: pass `last_modified_after` to extract only changed records.
- **Cancel and resume**: cancel aborts in-flight jobs and confirms the real state with Salesforce; resume re-creates jobs only for the incomplete objects of a failed scan.
- **Safe concurrency**: per-scan Postgres advisory locks stop cancel and the background task from overwriting each other, even across processes.
- **De-duplication and Parquet output**: records are de-duplicated by `Id` and written to MinIO.
- **ClickHouse loading**: tables and typed `INSERT … SELECT s3()` statements are generated from the object catalog (`ReplacingMergeTree` on `LastModifiedDate`).
- **Dual-key HMAC auth** for internal callers (`core` and `engineer` keys, with the engineer key required for maintenance).
- **Fail-fast startup**: all configuration is validated before the service accepts traffic.
- **Health, quota and statistics endpoints** for monitoring.

---

## Repo structure

```
.
├── app/
│   ├── main.py                    # app + lifespan: validates config, wires clients onto app.state
│   ├── config.py                  # SalesforceServiceSettings + validate_settings()
│   ├── auth/
│   │   ├── hmac_auth.py           # verify_hmac, require_key_id("engineer")
│   │   └── salesforce_auth.py     # JWT Bearer token manager
│   ├── clients/
│   │   ├── bulk_api_client.py     # Bulk API 2.0 wrapper
│   │   └── core_service_client.py # HMAC-signed status callback to core-service
│   ├── models/
│   │   ├── scan.py                
│   │   ├── objects.py             
│   │   └── clickhouse_schemas.py  
│   ├── routers/                   # Routes for APIs
│   ├── services/
│   │   ├── extraction_service.py  # Job lifecycle orchestration
│   │   ├── deduplication_service.py
│   │   └── maintenance.py         
│   └── storage/
│       ├── scan_store.py          
│       └── minio_client.py        
├── tests/
│   ├── unit/                      
│   └── integration/               
├── Dockerfile
├── docker-compose.yml            
├── pytest.ini
├── requirements.txt

```

To add a Salesforce object, add a `SupportedObject` entry in `models/objects.py`. The SOQL, the ClickHouse table and `/api/objects` are all derived from it. Fields that aren't plain strings (dates, amounts, booleans) go in `FIELD_KINDS` in `models/clickhouse_schemas.py`.

---

## Requirements

**Runtime**
- Python 3.13+
- PostgreSQL
- MinIO 
- ClickHouse 24.8
- Docker

**Salesforce**
- A **Connected App** with OAuth 2.0 JWT Bearer enabled and your public certificate uploaded
- An integration user approved for that app, with API Enabled and read access to the extracted objects
- The RSA private key (PEM) that matches the uploaded certificate

**Python packages**: see `requirements.txt`.
---

## Installation 

### Option A: Docker Compose (recommended)

1. Place your private key where compose mounts it:

   ```bash
   mkdir -p app/secrets
   cp /path/to/private.pem app/secrets/sf_private_key.pem
   ```

2. Export your Salesforce credentials (or put them in a `.env` next to `docker-compose.yml`):

   ```bash
   export SF_CONSUMER_KEY="3MVG9..."
   export SF_USERNAME="integration@yourorg.com"
   ```

3. If you are using a sandbox, change `SF_LOGIN_URL` in `docker-compose.yml` to `https://test.salesforce.com` (it defaults to production).

### Option B: Run the app locally

1. Create a virtualenv and install dependencies:

   ```bash
   python -m venv .venv
   source .venv/bin/activate          # Windows: .venv\Scripts\activate
   pip install -r requirements-dev.txt
   ```

2. Start Postgres, MinIO and ClickHouse (for example `docker compose up db minio minio-init clickhouse`).

3. Create a `.env` file in the repo root with the variables from the [reference below](#environment-variable-reference), pointing hosts at `localhost`. Set `SF_PRIVATE_KEY_PEM_PATH` to the local path of your PEM.

---

## Running the service and tests

### Run the service

```bash
# Docker Compose (everything)
docker compose up --build

# Local (after Option B)
fastapi run app/main.py --host 0.0.0.0 --port 5710
```

Check it's up:

```bash
curl http://localhost:5710/api/health
# {"status":"healthy","service":"salesforce_service","version":"1.0.0",
#  "salesforce_connected":true,"minio_connected":true}
```

- Interactive API docs: <http://localhost:5710/docs>
- MinIO console: <http://localhost:9001> (`minioadmin` / `minioadmin`)

If configuration is missing or invalid, the service logs a `CONFIGURATION VALIDATION FAILED` block (variable, error, fix) and refuses to start.

### Run the tests

Run pytest from the repo root. `pytest.ini` puts `app/` on the import path.

```bash
python -m pytest                        # everything
python -m pytest tests/unit -vv         # unit tests only (no external services)
python -m pytest tests/integration -vv  # integration tests only
```

---

## Environment variable reference

Loaded from the environment or a `.env` file in the repo root. Every variable is required unless a default is listed. Example values are the dev values from `docker-compose.yml`.

### App

```bash
FASTAPI_ENV=development
FASTAPI_DEBUG=True
SECRET_KEY=Any 32+ Chars
PORT=5710
HOST=0.0.0.0
ENVIRONMENT=dev
LOG_LEVEL=DEBUG
LOG_FORMAT=text
LOKI_ENABLED=false
DB_HOST=localhost
DB_PORT=5432
DB_NAME=salesforce_dev
DB_USER=dev_user
DB_PASSWORD=dev_pass
DB_SCHEMA=public
SF_CONSUMER_KEY=your-sf-consumer-key
SF_PRIVATE_KEY_PEM_PATH=your-private-key-path
SF_USERNAME=your-sf-username
SF_LOGIN_URL=https://test.salesforce.com
SF_API_VERSION=v59.0
SF_BULK_PAGE_SIZE=50000
SF_MAX_JOB_TIMEOUT_HOURS=2
MAX_CONCURRENT_SCANS=2
SCAN_TIMEOUT_HOURS=2
CLEANUP_DAYS=7
HMAC_ENABLED=true
HMAC_SECRET_KEY_CORE=your-hmac-core-secret-key
HMAC_SECRET_KEY_ENGINEER=your-hmac-engineer-secret-key
HMAC_SIGNATURE_MAX_AGE=300
MINIO_ENABLED=true
MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=your-minio-access-key
MINIO_SECRET_KEY=your-minio-secret-key
MINIO_SECURE=false
MINIO_BUCKET=name-of-bucket
CLICKHOUSE_ENABLED=true
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8123
CLICKHOUSE_USER=your_user
CLICKHOUSE_PASSWORD=your_pass
CLICKHOUSE_DATABASE=name-of-clickhouse-db
CLICKHOUSE_S3_ENDPOINT=http://minio:9000
CLICKHOUSE_SECURE=true
ALLOWED_ORIGINS=*
CORE_SERVICE_URL=your-core-service-url

```

## Quick API overview

### Authentication

When `HMAC_ENABLED=true`, every endpoint except `/api/health` needs three headers:

| Header | Value |
|---|---|
| `X-Timestamp` | Unix time in seconds |
| `X-Signature` | `hex(HMAC-SHA256(secret, f"{timestamp}.{raw_body}"))` |
| `X-Key-Id` | `core` (default) or `engineer` |

Sign the body exactly as sent (an empty string for bodyless requests). Signatures older than `HMAC_SIGNATURE_MAX_AGE` are rejected.

```python
import hashlib, hmac, json, time, requests

SECRET, BASE = "...", "http://localhost:5710"   # SECRET = HMAC_SECRET_KEY_CORE

def signed(body="", key_id="core"):
    ts = str(time.time())
    sig = hmac.new(SECRET.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return {"X-Timestamp": ts, "X-Signature": sig, "X-Key-Id": key_id, "Content-Type": "application/json"}

body = json.dumps({"scan_id": "scan-001", "org_id": "org-1", "objects": ["Contact", "Account"]})
print(requests.post(f"{BASE}/api/scan/start", data=body, headers=signed(body)).json())
```

### Endpoints

| Method | Path | Key | Description |
|---|---|---|---|
| GET | `/api/health` | none | Checks the Salesforce token and MinIO bucket |
| GET | `/api/key/verify` | any | Validates your signature; returns the key's permissions |
| POST | `/api/scan/start` | any | Create Bulk API jobs and start extracting (**202**) |
| GET | `/api/scan/list` | any | List scans; optional `status` and `org_id` filters |
| GET | `/api/scan/{scan_id}/status` | any | Scan state, per-object progress and totals |
| POST | `/api/scan/{scan_id}/cancel` | any | Abort in-flight jobs (idempotent) |
| POST | `/api/scan/{scan_id}/resume` | any | Re-run incomplete objects of a **failed** scan |
| DELETE | `/api/scan/{scan_id}/remove` | any | Delete the scan record |
| GET | `/api/statistics` | any | Scan counts by status and total records extracted |
| POST | `/api/maintenance/cleanup` | `engineer` | Purge scans older than `CLEANUP_DAYS` (`?dry_run=true` to preview) |
| GET | `/api/objects` | any | Supported objects, SOQL templates and default fields |
| GET | `/api/batch/info` | any | Org info, daily API quota, active Bulk jobs, token expiry |

### Starting a scan

```json
POST /api/scan/start
{
  "scan_id": "scan-001",
  "org_id": "org-1",
  "objects": ["Contact", "Account", "Opportunity"],
  "filters": { "last_modified_after": "2026-01-01T00:00:00Z" }
}
```

`scan_id` is supplied by the caller. `filters` is optional (omit it for a full extraction). An unsupported object name returns **400** and creates nothing. Poll `GET /api/scan/{scan_id}/status` for progress.

**Scan statuses:** `started`, `in progress`, `complete`, `failed`, `cancelled`.
**Object (Salesforce job) states:** `Pending`, `UploadComplete`, `InProgress`, `JobComplete`, `Failed`, `Aborted`.

Results land in MinIO at `{org_id}/{scan_id}/{object}/page_0001.parquet` (object name lowercased) and in a ClickHouse table named after the object (`contact`, `account`, `opportunity`).