import logging
from fastapi import APIRouter, BackgroundTasks, Request, HTTPException
from clients.bulk_api_client import SalesforceBulkAPIClient
from pydantic import BaseModel
from services import extraction_service

router = APIRouter()
logger = logging.getLogger(__name__)

class ScanStartRequest(BaseModel):
     scan_id: str
     org_id: str
     objects: list[str]
     filters: dict | None = None

@router.post("/api/scan/start", status_code=202)
def start_scan(req: ScanStartRequest, request : Request, background_tasks: BackgroundTasks):
    bulk_client = request.app.state.bulk_client
    scan_store = request.app.state.scan_store
    minio_client = request.app.state.minio_client
    last_modified_after = (req.filters or {}).get("last_modified_after")

    try:
        scan = extraction_service.start_scan(
            bulk_client, 
            scan_store, 
            req.scan_id, 
            req.org_id, 
            req.objects, 
            last_modified_after
        )
    
    except Exception as e:
        logger.error(f"Error occurred while starting scan: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
    background_tasks.add_task(
        extraction_service.run_scan_background, bulk_client, scan_store, req.scan_id, minio_client
    )
    
    return {
            "success": True,
            "scan_id": scan.scan_id,
            "status": "started",
            "jobs": {name: p.sf_job_id for name, p in scan.progress.items()},
        }