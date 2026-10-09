import logging
from datetime import datetime, timezone
from fastapi import APIRouter, BackgroundTasks, Query, Request, HTTPException, Depends
from pydantic import BaseModel
from models.scan import ObjectProgress, ScanStatus
from services import extraction_service

from models.scan import ScanState
from auth.hmac_auth import verify_hmac

router = APIRouter(dependencies=[Depends(verify_hmac)])
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
    settings = request.app.state.settings

    try:
        objects = extraction_service.resolve_objects(req.objects)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    scan = extraction_service.start_scan(
        bulk_client, scan_store, req.scan_id, req.org_id, objects, last_modified_after
    )

    core_service_client = request.app.state.core_client
    clickhouse_client = request.app.state.clickhouse_client

    background_tasks.add_task(
        extraction_service.run_scan_background, bulk_client, scan_store, req.scan_id, minio_client, 
        settings.SF_MAX_JOB_TIMEOUT_HOURS * 3600, core_service_client, clickhouse_client
    )

    logger.info(f"Scan details: {scan}")
    
    return {
            "scan_details": scan,
            "success": True,
            "scan_id": scan.scan_id,
            "status": "started",
            "jobs": {name: p.sf_job_id for name, p in scan.progress.items()},
            "message": f"{len(scan.progress)} Bulk API jobs created. Poll /api/scan/{scan.scan_id}/status for progress.",
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
        "records_extracted": sum(p.records_processed for p in scan.progress.values())
    }

    return {**scan.model_dump(), "totals": totals}

'''
Aborts all in-progress Salesforce Bulk API jobs for this scan. 
Records partial results already written to MinIO.
'''
@router.post("/api/scan/{scan_id}/cancel")
def cancel_scan(request: Request, scan_id: str):
    bulk_client = request.app.state.bulk_client
    scan_store = request.app.state.scan_store

    with scan_store.lock(scan_id):
        scan = scan_store.get(scan_id)
        if not scan:
            raise HTTPException(status_code=404, detail="Scan not found")
        
        if scan.status == ScanStatus.CANCELLED:
            return {
                "success": True, 
                "scan_id": scan.scan_id, 
                "status": scan.status, 
                "message": "Already cancelled"
            }
        
    jobs_aborted, jobs_already_complete, jobs_failed_to_abort = [], [], []

    for obj_name, progress in scan.progress.items():
        if progress.state in ("JobComplete"):
            jobs_already_complete.append(progress.sf_job_id)
            continue
        if progress.state in ("Failed", "Aborted"):
            continue

        try:
            bulk_client.abort_job(progress.sf_job_id)
            actual_state = bulk_client.get_job_info(progress.sf_job_id)["state"]
        except Exception as e:
            logger.warning(f"Failed to abort job {progress.sf_job_id}: {e}")
            jobs_failed_to_abort.append(progress.sf_job_id)
            continue
        
        progress.state = actual_state
        logger.info(f"Job {progress.sf_job_id} state after abort: {actual_state}")
        print(f"Job {progress.sf_job_id} state after abort: {actual_state}")
        if (actual_state == "Aborted"):
            jobs_aborted.append(progress.sf_job_id)
        else:
            # PATCH succeeded but SF didn't confirm Aborted (e.g. raced JobComplete) — trust the GET
            jobs_failed_to_abort.append(progress.sf_job_id)

    if (len(jobs_aborted) == len(scan.progress)):
        scan.status = ScanStatus.CANCELLED
    scan.updated_at = datetime.now(timezone.utc)
    scan_store.update(scan)

    complete_names = [
        name for name, progress in scan.progress.items()
        if progress.state == "JobComplete"
    ]

    message = (
        f"Scan cancelled. {len(complete_names)} object(s) "
        f"({', '.join(complete_names) or 'none'}) completed before cancellation."
    )
    if jobs_failed_to_abort:
        message += f" {len(jobs_failed_to_abort)} job(s) failed to abort and may still be running."

    success = bool(len(jobs_aborted) > 0) # fail if failed to abort? or if there were exceptions ?
    status = "cancelled" if success == True else "failed" # 

    return {
        "success": success,
        "scan_id": scan.scan_id,
        "status": status,
        "jobs_aborted": jobs_aborted,
        "jobs_already_complete": jobs_already_complete,
        "jobs_failed_to_abort": jobs_failed_to_abort,
        "message": message,
    }

'''Resume a failed scan by re-running extraction for incomplete objects only'''
@router.post("/api/scan/{scan_id}/resume")
def resume_scan(request: Request, scan_id: str, background_tasks: BackgroundTasks):
    bulk_client = request.app.state.bulk_client
    scan_store = request.app.state.scan_store
    minio_client = request.app.state.minio_client
    settings = request.app.state.settings

    with scan_store.lock(scan_id):
        scan = scan_store.get(scan_id)
        if not scan:
            raise HTTPException(status_code=404, detail="Scan not found")
        
        if scan.status not in (ScanStatus.FAILED):
            raise HTTPException(
                status_code=400, 
                detail=f"Only failed scans can be resumed (current status: {scan.status})"
            )
        
        incomplete = [
            name for name, p in scan.progress.items()
            if p.state not in ("JobComplete", "Aborted")
        ]

        if not incomplete:
            raise HTTPException(
                status_code=400,
                detail= "No incomplete objects to resume"
            )
        
        resumed = []
        for obj_name in incomplete:
            soql = extraction_service.build_soql(obj_name, scan.last_modified_after)
            new_job_id = bulk_client.create_query_job(soql)
            scan.progress[obj_name] = ObjectProgress(sf_job_id=new_job_id, state="UploadComplete")
            resumed.append(obj_name)
        
        scan.status = ScanStatus.IN_PROGRESS
        scan.updated_at = datetime.now(timezone.utc)
        scan_store.update(scan) # Status should be IN PROGRESS by now

        core_service_client = request.app.state.core_client
        clickhouse_client = request.app.state.clickhouse_client

        background_tasks.add_task(
            extraction_service.run_scan_background, bulk_client, scan_store, scan_id, minio_client, 
            settings.SF_MAX_JOB_TIMEOUT_HOURS * 3600, core_service_client, clickhouse_client
        )

    return {
        "success": True,
        "scan_id": scan_id,
        "status": scan_store.get(scan_id).status,
        "resumed_objects": resumed,
        "message": f"Re-created jobs for {len(resumed)} incomplete object(s): {', '.join(resumed)}",
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