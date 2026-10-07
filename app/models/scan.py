 # Scan state model

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel

class ScanStatus(str, Enum):
    STARTED = "started"
    IN_PROGRESS = "in progress"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"

class ObjectProgress(BaseModel):
    sf_job_id: str | None = None
    state: str = "Pending"
    records_processed: int = 0
    records_failed: int = 0
    pages_downloaded: int = 0
    minio_path: list[str] | None = None
    error: str | None = None
    clickhouse_loaded: bool = False
    clickhouse_rows: int = 0
    clickhouse_error: str | None = None

class ScanState(BaseModel):
    scan_id: str
    org_id: str
    status: ScanStatus = ScanStatus.STARTED
    started_at: datetime = datetime.now(timezone.utc)
    updated_at: datetime = datetime.now(timezone.utc)
    progress: dict[str, ObjectProgress] = {}
    last_modified_after: str | None = None