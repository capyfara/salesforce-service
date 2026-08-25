import logging
from fastapi import APIRouter, BackgroundTasks, Query, Request, HTTPException
from pydantic import BaseModel
from services import extraction_service

router = APIRouter()
logger = logging.getLogger(__name__)

class ScanStartRequest(BaseModel):
     scan_id: str
     org_id: str
     objects: list[str]
     filters: dict | None = None


'''Start a Salesforce extraction scan'''
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


'''List all scans with filters'''
@router.get("/api/scan/list")
def list_scans(request: Request, status: str | None = Query(default=None), org_id: str | None = Query(default=None)):
    print(f"DEBUG: received status={status!r}, org_id={org_id!r}")
    scans = request.app.state.scan_store.list_all()
    if status:
        print(f"DEBUG: available statuses = {[s.status for s in scans]}")
        scans = [s for s in scans if s.status == status] # Not applying correctly
    if org_id:
        scans = [s for s in scans if s.org_id == org_id]
    return {"scans": scans, "count": len(scans)}


'''Get scan status and progress'''
@router.get("/api/scan/{scan_id}/status")
def get_scan_status(scan_id: str, request: Request):
    scan = request.app.state.scan_store.get(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")
    
    totals = {
        "objects_total": len(scan.progress),
        "objects_complete": sum(1 for p in scan.progress.values() if p.state == "JobComplete"),
        "objects_failed": sum(1 for p in scan.progress.values() if p.state == "Failed"),
        "objects_aborted": sum(1 for p in scan.progress.values() if p.state == "Aborted"),
        "records_extracted": sum(p.records_processed for p in scan.progress.values())
    }

    return {**scan.model_dump(), "totals": totals}

'''Cancel an in-progress scan'''
@router.post("/api/scan/{scan_id}/cancel")
def cancel_scan(request: Request, scan_id: str):
    bulk_client = request.app.state.bulk_client
    scan_store = request.app.state.scan_store

    scan = scan_store.get(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")
    
    jobs_aborted=[]
    jobs_already_complete=[]

    for obj_name, progress in scan.progress.items():
        if progress.state in ("JobComplete"):
            jobs_already_complete.append(progress.sf_job_id)
            continue

        bulk_client.delete_job(progress.sf_job_id) # Failing to delete job
        progress.state = "aborted" # Not updating job state
        jobs_aborted.append(progress.sf_job_id)

    scan.status = "aborted"
    scan_store.update(scan)

    complete_names = [
        name for name, p in scan.progress.items()
        if p.sf_job_id in jobs_already_complete
    ]

    message = (
        f"Scan cancelled. {len(complete_names)} object(s) "
        f"({', '.join(complete_names) or 'none'}) completed before cancellation."
    )

    return {
        "success": True,
        "scan_id": scan.scan_id,
        "status": "cancelled",
        "jobs_aborted": jobs_aborted,
        "jobs_already_complete": jobs_already_complete,
        "message": message
    }


'''Remove a scan record'''
@router.delete("/api/scan/{scan_id}/remove")
def remove_scan(request: Request, scan_id: str):
    logger.info(f"Attempting to remove scan: {scan_id}")
    scan_store = request.app.state.scan_store
    scan = scan_store.get(scan_id)
    if not scan:
        logger.warning(f"Scan not found: {scan_id}")
        raise HTTPException(status_code=404, detail="Scan not found")
    
    scan_store.delete(scan_id)
    if (scan_store.get(scan_id)):
        logger.error(f"Failed to remove scan: {scan_id}")
        raise HTTPException(status_code=500, detail="Failed to remove scan")

    logger.info(f"Scan removed successfully: {scan_id}")
    return {"message": "Scan removed successfully"}


'''Aggregate scan stats'''
@router.get("/api/statistics")
def get_statistics(request: Request):
    scans = request.app.state.scan_store.list_all()
    total_records = sum (
        p.records_processed for s in scans for p in s.progress.values()
    )
    return {
        "total scans": len(scans),
        "by_status": {
            status: sum(1 for s in scans if s.status == status) for status in {s.status for s in scans}
        },
        "total_records_extracted": total_records
    }