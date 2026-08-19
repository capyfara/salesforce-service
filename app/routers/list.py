'''List all scans with filters'''

import logging
from fastapi import APIRouter, Request, Query

logger = logging.getLogger(__name__)
router = APIRouter()

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