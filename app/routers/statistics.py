'''Aggregate scan stats'''

from fastapi import APIRouter, Request, Depends
from auth.hmac_auth import verify_hmac

router = APIRouter(dependencies=[Depends(verify_hmac)])

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