'''Remove a scan record'''

import logging
from fastapi import Request, APIRouter, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter()

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