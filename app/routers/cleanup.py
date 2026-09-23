import logging
from fastapi import APIRouter, Request, Query, Depends

from auth.hmac_auth import require_key_id
from services import maintenance

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post(
    "/api/maintenance/cleanup",
    dependencies=[Depends(require_key_id("engineer"))],
)
def cleanup(request: Request, dry_run: bool = Query(default=False)):
    """
    Purge scan records past the retention window (settings.CLEANUP_DAYS)
    Engineer-key required
    """
    scan_store = request.app.state.scan_store
    settings = request.app.state.settings

    result = maintenance.cleanup_old_scans(
        scan_store,
        cleanup_days=settings.CLEANUP_DAYS,
        dry_run=dry_run,
    )

    logger.info(f"Maintenance cleanup triggered (dry_run={dry_run}): {result}")

    return {"success": True, **result}