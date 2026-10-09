"""
Orchestrates bulk job lifecycle

1. Take scan request
2. Build a SOQL query per object
3. Create a bulk api job per object
4. Track per-object state in ScanState
5. Do the actual polling and downloading in the background
6. Update state as each other's object progresses/completes/fails

"""
import logging

from datetime import datetime, timezone

from models.objects import SUPPORTED_OBJECTS
from models.scan import ObjectProgress, ScanState, ScanStatus

from config import SalesforceServiceSettings

from services.deduplication_service import RecordDeduplicator

from fastapi import HTTPException

logger = logging.getLogger(__name__)

SF_RESULT_RETENTION_DAYS = 7

OBJECT_ALIASES = {"Activity": ["Task", "Event"]}
_BY_NAME = {o.name: o for o in SUPPORTED_OBJECTS}

def resolve_objects(objects: list[str]) -> list[str]:
    """Expand aliases, dedupe (order-preserving), and validate names."""
    resolved: list[str] = []
    for name in objects:
        for n in OBJECT_ALIASES.get(name, [name]):
            if n not in _BY_NAME:
                raise ValueError(f"Unsupported object: {n}")
            if n not in resolved:
                resolved.append(n)
    return resolved


def build_soql(object_name: str, last_modified_after: str | None) ->  str:
    obj = next(o for o in SUPPORTED_OBJECTS if o.name == object_name)
    conditions = []
    if obj.base_where:
        conditions.append(obj.base_where)
    if last_modified_after:
        conditions.append(f"LastModifiedDate >= {last_modified_after}")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    fields = ", ".join(obj.default_fields)
    return obj.soql_template.format(fields=fields, where_clause=where_clause)
    
    # fields = ", ".join(obj.default_fields)
    # where_clause = f"WHERE LastModifiedDate >= {last_modified_after}" if last_modified_after else ""
    # return obj.soql_template.format(fields=fields, where_clause=where_clause)

def start_scan(bulk_client, scan_store, scan_id: str, org_id: str, objects: list[str], last_modified_after: str | None) -> ScanState:
    """Creates Bulk API Jobs for each requested object"""
    scan = ScanState(
        scan_id=scan_id, 
        org_id=org_id, 
        last_modified_after=last_modified_after
    )

    for obj_name in objects:
        soql = build_soql(obj_name, last_modified_after)
        job_id = bulk_client.create_query_job(soql)
        scan.progress[obj_name] = ObjectProgress(sf_job_id=job_id, state="UploadComplete")
    
    scan.status = ScanStatus.IN_PROGRESS
    scan_store.create(scan)
    return scan

def _set_progress(scan_store, scan_id, obj_name, **fields):
    with scan_store.lock(scan_id):
        scan = scan_store.get(scan_id)
        if scan is None or scan.status == ScanStatus.CANCELLED:
            return False
        for k, v in fields.items():
            setattr(scan.progress[obj_name], k, v)
        scan_store.update(scan)
        return True

def run_scan_background(bulk_client, scan_store, scan_id: str, minio_client, max_timeout_seconds: int, core_service_client, clickhouse_client):
    """Poll each job to completion, download results"""

    scan = scan_store.get(scan_id)
    if not scan:
        return
    
    for obj_name, progress in scan.progress.items():
        if progress.state == "JobComplete":
            # already done, don't touch it
            continue

        with scan_store.lock(scan_id):
            current = scan_store.get(scan_id)
            if current is None or current.status == ScanStatus.CANCELLED:
                return # If scan is cancelled, stop polling

        # Start polling 
        try:
            result = bulk_client.poll_until_complete(progress.sf_job_id, max_timeout_seconds)
            
            # Check if scan was cancelled after polling
            with scan_store.lock(scan_id):
                scan = scan_store.get(scan_id)
                if scan.status == ScanStatus.CANCELLED:
                    return
                progress = scan.progress[obj_name]
                progress.state = result.state
                progress.records_processed = result.records_processed
                progress.records_failed = result.records_failed
                scan_store.update(scan)

            # Check if scan was complete during/after polling
            if result.state == "JobComplete":
                
                logger.info(f"Scan completed for object: {progress}")

                deduplicator = RecordDeduplicator(id_field="Id")
                for page in bulk_client.iter_results(progress.sf_job_id):
                    deduped_page = deduplicator.deduplicate(page)
                    progress.pages_downloaded += 1

                    path = minio_client.upload_records(
                        records=deduped_page,
                        org_id=scan.org_id,
                        scan_id=scan.scan_id,
                        object_name=obj_name,
                        page_num=progress.pages_downloaded,
                    )
                    if progress.minio_path is None:
                        progress.minio_path = []
                    if path:
                        progress.minio_path.append(path)
                    scan_store.update(scan)
                
                # Delete SF Job
                bulk_client.delete_job(progress.sf_job_id)
            else:
                with scan_store.lock(scan_id):
                    scan = scan_store.get(scan_id)
                    progress = scan.progress[obj_name]
                    progress.error = result.error_message
                    scan_store.update(scan)

        except Exception as e:
            print(f"Error occurred while polling job: {e}")

            with scan_store.lock(scan_id):
                scan = scan_store.get(scan_id)
                if scan.status != ScanStatus.CANCELLED:
                    progress = scan.progress[obj_name]
                    progress.state = "Failed"
                    progress.error = str(e)
                    scan_store.update(scan)

            logger.error(f"Scan failed for object: {progress}")

    if clickhouse_client and progress.minio_path:   # empty -> s3() glob would error
        try:
            rows = clickhouse_client.load_object(obj_name, scan.org_id, scan.scan_id)
            _set_progress(scan_store, scan_id, obj_name,
                          clickhouse_loaded=True, clickhouse_rows=rows, clickhouse_error=None)
        except Exception as e:
            logger.error(f"ClickHouse load failed for {obj_name}: {e}")
            _set_progress(scan_store, scan_id, obj_name,
                          clickhouse_loaded=False, clickhouse_error=str(e))

    bulk_client.delete_job(progress.sf_job_id)
        

    # Update scan status once all jobs reach terminal state
    with scan_store.lock(scan_id):
        scan = scan_store.get(scan_id)
        if scan.status != ScanStatus.CANCELLED:
            all_states = [p.state for p in scan.progress.values()]
            if any(s == "Aborted" for s in all_states):
                scan.status = ScanStatus.CANCELLED  # shouldn't normally reach here if cancel_scan's lock works, but safe fallback
            else:
                scan.status = ScanStatus.COMPLETE if all(s == "JobComplete" for s in all_states) else ScanStatus.FAILED
            scan_store.update(scan)


            # *COMMENTED OUT AS DEV ENVIRONMENT DOES NOT HAVE CORE SERVICE AVAILABLE*

            # Report scan outcome to Core Service Client
            
            # try:
            #     core_service_client.report_scan_status(scan_id, scan.status, deduped_page)
            #     pass
            # except Exception as e:
            #     print(f"Error occurred while reporting scan status: {e}")
            #     HTTPException(status_code=500, detail="Error occurred while reporting scan status to Core Service Client")