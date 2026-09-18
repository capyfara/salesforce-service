'''Salesforce org and API quota info'''

import logging
from fastapi import APIRouter, Request, HTTPException, Depends
from models.scan import ScanStatus
from auth.hmac_auth  import verify_hmac

router = APIRouter(dependencies=[Depends(verify_hmac)])
logger = logging.getLogger(__name__)

MAX_CONCURRENT_JOBS = 100  # Salesforce Bulk API hard limit

@router.get("/api/batch/info")
def get_batch_info(request: Request):
    bulk_client = request.app.state.bulk_client
    token_manager = request.app.state.token_manager
    scan_store = request.app.state.scan_store
    settings = request.app.state.settings

    try:
        org_info = bulk_client.get_org_info()
        limits = bulk_client.get_org_limits()
    except Exception as e:
        logger.error(f"Failed to fetch Salesforce org/limits info: {e}")
        raise HTTPException(status_code=502, detail=f"Salesforce API error: {e}")

    daily = limits.get("DailyApiRequests", {})

    active_jobs = sum(
        1
        for scan in scan_store.list_all()
        if scan.status == ScanStatus.IN_PROGRESS
        for p in scan.progress.values()
        if p.state in ("UploadComplete", "InProgress")
    )

    return {
        "org": org_info,
        "api_limits": {
            "daily_api_requests": {
                "max": daily.get("Max"),
                "remaining": daily.get("Remaining"),
            },
            "bulk_api_jobs": {
                "active": active_jobs,
                "max_concurrent": MAX_CONCURRENT_JOBS,
            },
        },
        "token_expires_in_seconds": token_manager.seconds_until_expiry(),
    }