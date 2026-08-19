'''Get scan status and progress'''

from fastapi import APIRouter, Request, HTTPException

router =  APIRouter()

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