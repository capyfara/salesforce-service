"""
Purges old scan records that have reached a terminal state and are
older than the configured retention window (CLEANUP_DAYS).
"""
import logging
from datetime import datetime, timedelta, timezone

from models.scan import ScanStatus

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {ScanStatus.COMPLETE, ScanStatus.FAILED, ScanStatus.CANCELLED}


def cleanup_old_scans(scan_store, cleanup_days: int, dry_run: bool = False) -> dict:
    """
    Removes scan records whose last update is older than `cleanup_days`
    and whose status is terminal. In-progress scans are always skipped,
    regardless of age.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=cleanup_days)

    removed, skipped_active, skipped_recent = [], [], []

    for scan in scan_store.list_all():
        if scan.status not in TERMINAL_STATUSES:
            skipped_active.append(scan.scan_id)
            continue

        if scan.updated_at >= cutoff:
            skipped_recent.append(scan.scan_id)
            continue

        removed.append(scan.scan_id)
        if not dry_run:
            with scan_store.lock(scan.scan_id):
                scan_store.delete(scan.scan_id)

    logger.info(
        f"Cleanup {'(dry run) ' if dry_run else ''}complete: "
        f"{len(removed)} removed, {len(skipped_active)} active skipped, "
        f"{len(skipped_recent)} within retention window"
    )

    return {
        "removed_count": len(removed),
        "removed_scan_ids": removed,
        "skipped_active_count": len(skipped_active),
        "skipped_recent_count": len(skipped_recent),
        "cutoff": cutoff.isoformat(),
        "cleanup_days": cleanup_days,
        "dry_run": dry_run,
    }