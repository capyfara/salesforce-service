# mock_salesforce/main.py
import base64
import csv
import io
import uuid

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

app = FastAPI()

API = "/services/data/v59.0"
INSTANCE_URL = "http://mock-salesforce:8080"
PAGE_SIZE = 50000

# ---------------------------------------------------------------------------
# Fault injection state
# ---------------------------------------------------------------------------
fault = {"remaining": 0, "status": 403, "code": "REQUEST_LIMIT_EXCEEDED"}

# Paths that never get faults (so token refresh and healthchecks stay clean)
FAULT_EXEMPT = ("/_fault", "/health", "/services/oauth2/token")

# job_id -> {"object": str, "polls": int, "state": str}
jobs: dict[str, dict] = {}

# How many records each fake object "has"
RECORD_COUNTS = {"Contact": 120, "Account": 80, "Opportunity": 60}

FIELDS = {
    "Contact": ["Id", "Name", "Email", "Phone", "AccountId", "OwnerId", "CreatedDate", "LastModifiedDate"],
    "Account": ["Id", "Name", "Type", "Industry", "BillingCity", "BillingState", "OwnerId", "CreatedDate", "LastModifiedDate"],
    "Opportunity": ["Id", "Name", "AccountId", "OwnerId", "StageName", "Amount", "CloseDate", "CreatedDate", "LastModifiedDate"],
}


@app.middleware("http")
async def inject_faults(request: Request, call_next):
    if not request.url.path.startswith(FAULT_EXEMPT) and fault["remaining"] > 0:
        fault["remaining"] -= 1
        return JSONResponse(
            [{"errorCode": fault["code"], "message": "simulated fault"}],
            status_code=fault["status"],
            headers={"Retry-After": "2"},
        )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Control endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/_fault")
def set_fault(f: dict):
    fault.update(f)
    return fault


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------
@app.post("/services/oauth2/token")
async def token():
    return {
        "access_token": "00Dmock!fake-token",
        "instance_url": INSTANCE_URL,
        "token_type": "Bearer",
    }


# ---------------------------------------------------------------------------
# Bulk API 2.0 (query)
# ---------------------------------------------------------------------------
def _object_from_soql(soql: str) -> str:
    parts = soql.split()
    return parts[parts.index("FROM") + 1]


def _make_rows(obj: str, count: int):
    fields = FIELDS.get(obj, ["Id", "LastModifiedDate"])
    for i in range(count):
        row = {}
        for f in fields:
            if f == "Id":
                row[f] = f"{obj[:3].upper()}{i:012d}"
            elif f.endswith("Date"):
                row[f] = "2026-04-30T14:22:00.000+0000"
            elif f == "Amount":
                row[f] = str(1000 + i)
            else:
                row[f] = f"{f}-{i}"
        yield row

# def create_query_job(self, soql: str) -> str:
#     url = f"{self._base_url()}{API}"
#     payload = {"operation": "query", "query": soql, "contentType": "CSV",
#                 "columnDelimiter": "COMMA", "lineEnding": "LF"}
#     resp = self._request("POST", url, json=payload, idempotent=False)
#     return resp.json()["id"]

def invalidate(self):
    with self._lock:
        self._expires_at = 0

@app.post(f"{API}/jobs/query", status_code=200)
async def create_job(request: Request):
    body = await request.json()
    obj = _object_from_soql(body["query"])
    job_id = "750" + uuid.uuid4().hex[:15]
    jobs[job_id] = {"object": obj, "polls": 0, "state": "UploadComplete"}
    return {"id": job_id, "operation": "query", "object": obj,
            "state": "UploadComplete", "contentType": "CSV", "apiVersion": 59.0}


@app.get(f"{API}/jobs/query/{{job_id}}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return JSONResponse([{"errorCode": "NOT_FOUND", "message": "job not found"}], status_code=404)

    # Simulate progress: InProgress on first poll, JobComplete on the second
    if job["state"] not in ("Aborted", "Failed", "JobComplete"):
        job["polls"] += 1
        job["state"] = "InProgress" if job["polls"] < 2 else "JobComplete"

    total = RECORD_COUNTS.get(job["object"], 10)
    return {
        "id": job_id,
        "state": job["state"],
        "object": job["object"],
        "numberRecordsProcessed": total if job["state"] == "JobComplete" else 0,
        "numberRecordsFailed": 0,
    }


@app.get(f"{API}/jobs/query/{{job_id}}/results")
def get_results(job_id: str, maxRecords: int = PAGE_SIZE, locator: str | None = None):
    job = jobs.get(job_id)
    if not job:
        return JSONResponse([{"errorCode": "NOT_FOUND", "message": "job not found"}], status_code=404)

    total = RECORD_COUNTS.get(job["object"], 10)
    offset = int(base64.b64decode(locator).decode()) if locator else 0
    end = min(offset + maxRecords, total)

    rows = list(_make_rows(job["object"], total))[offset:end]
    fields = FIELDS.get(job["object"], ["Id", "LastModifiedDate"])

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)

    next_locator = base64.b64encode(str(end).encode()).decode() if end < total else "null"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Sforce-Locator": next_locator, "Sforce-NumberOfRecords": str(len(rows))},
    )


@app.patch(f"{API}/jobs/query/{{job_id}}")
async def abort_job(job_id: str, request: Request):
    body = await request.json()
    job = jobs.get(job_id)
    if job and body.get("state") == "Aborted":
        job["state"] = "Aborted"
    return {"id": job_id, "state": job["state"] if job else "Aborted"}


@app.delete(f"{API}/jobs/query/{{job_id}}", status_code=204)
def delete_job(job_id: str):
    jobs.pop(job_id, None)
    return Response(status_code=204)