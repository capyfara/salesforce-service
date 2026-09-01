"""
Orchestrates bulk job lifecycle

1. Take scan request
2. Build a SOQL query per object
3. Create a bulk api job per object
4. Track per-object state somewhere
5. Do the actual polling and downloading in the background
6. Update state as each other's object progresses/completes/fails

"""
from datetime import datetime, timezone
from models.objects import SUPPORTED_OBJECTS
from models.scan import ObjectProgress, ScanState, ScanStatus

SF_RESULT_RETENTION_DAYS = 7

def build_soql(object_name: str, last_modified_after: str | None) ->  str:
    obj = next(o for o in SUPPORTED_OBJECTS if o.name == object_name)
    fields = ", ".join(obj.default_fields)
    where_clause = f"WHERE LastModifiedDate >= {last_modified_after}" if last_modified_after else ""
    return obj.soql_template.format(fields=fields, where_clause=where_clause)

def start_scan(bulk_client, scan_store, scan_id: str, org_id: str, objects: list[str], last_modified_after: str | None) -> ScanState:
    """Creates Bulk API Jobs for each requested object"""
    scan = ScanState(scan_id=scan_id, org_id=org_id)

    for obj_name in objects:
        soql = build_soql(obj_name, last_modified_after)
        job_id = bulk_client.create_query_job(soql)
        scan.progress[obj_name] = ObjectProgress(sf_job_id=job_id, state="UploadComplete")
    
    scan.status = ScanStatus.IN_PROGRESS
    scan_store.create(scan)
    return scan

def run_scan_background(bulk_client, scan_store, scan_id: str, minio_client):
    """Poll each job to completion, download results"""

    scan = scan_store.get(scan_id)
    if not scan:
        return
    
    for obj_name, progress in scan.progress.items():
        with scan_store.lock(scan_id):
            current = scan_store.get(scan_id)
            if current is None or current.status == ScanStatus.CANCELLED:
                return # If scan is cancelled, stop polling
            
        try:
            # ACTUAL RESULT FROM SALESFORCE
            result = bulk_client.poll_until_complete(progress.sf_job_id)

            # Recheck after polling 
            with scan_store.lock(scan_id):
                scan = scan_store.get(scan_id)
                if scan.status == ScanStatus.CANCELLED:
                    return
                progress = scan.progress[obj_name]
                progress.state = result.state
                progress.records_processed = result.records_processed
                progress.records_failed = result.records_failed

            if result.state == "JobComplete":
                for page in bulk_client.iter_results(progress.sf_job_id):
                    progress.pages_downloaded += 1

                    path = minio_client.upload_records(
                        records=page,
                        org_id=scan.org_id,
                        scan_id=scan.scan_id,
                        object_name=obj_name,
                        page_num=progress.pages_downloaded,
                    )
                    if progress.minio_path is None:
                        progress.minio_path = []
                    if path:
                        progress.minio_path.append(path)

                bulk_client.delete_job(progress.sf_job_id)
            else:
                progress.error = result.error_message
        except Exception as e:
            with scan_store.lock(scan_id):
                scan = scan_store.get(scan_id)
                if scan.status != ScanStatus.CANCELLED:
                    progress = scan.progress[obj_name]
                    progress.state = "Failed"
                    progress.error = str(e)

        with scan_store.lock(scan_id):
            scan = scan_store.get(scan_id)
            if scan.status != ScanStatus.CANCELLED:
                all_states = [p.state for p in scan.progress.values()]
                if any(s == "Aborted" for s in all_states):
                    scan.status = ScanStatus.CANCELLED  # shouldn't normally reach here if cancel_scan's lock works, but safe fallback
                else:
                    scan.status = ScanStatus.COMPLETE if all(s == "JobComplete" for s in all_states) else ScanStatus.FAILED
                scan_store.update(scan)

def resume_scan(bulk_client, scan_store, scan_id: str, last_modified_after: str | None = None) -> ScanState:
    """
    Repairs a failed scan. For each object:
      - JobComplete on Salesforce -> results still exist for 7 days; just retry the download,
        don't create a new SF job.
      - Failed / Aborted / stuck -> the SF job is dead, submit a brand new query job.
    Objects that are already fully persisted (pages downloaded, no error) are left untouched.
    """
    with scan_store.lock(scan_id):
        scan = scan_store.get(scan_id)
        if scan is None:
            raise ValueError(f"scan {scan_id} not found")
        if scan.status not in (ScanStatus.FAILED, ScanStatus.CANCELLED):
            raise ValueError(f"scan {scan_id} is {scan.status}, nothing to resume")

        resumed_objects = []
        for obj_name, progress in scan.progress.items():
            if progress.state == "JobComplete" and not progress.error:
                # already fully done and persisted
                continue

            if progress.state == "JobComplete" and progress.error:
                progress.error = None
                progress.pages_downloaded = 0
                progress.minio_path = None
                # state stays "JobComplete" — run_scan_background will re-download it
                resumed_objects.append(obj_name)
                continue

            # Failed / Aborted / stuck-InProgress -> the SF job is dead. Resubmit fresh.
            soql = build_soql(obj_name, last_modified_after)
            new_job_id = bulk_client.create_query_job(soql)
            scan.progress[obj_name] = ObjectProgress(sf_job_id=new_job_id, state="UploadComplete")
            resumed_objects.append(obj_name)

        if not resumed_objects:
            raise ValueError(f"scan {scan_id} has nothing eligible to resume")

        scan.status = ScanStatus.IN_PROGRESS
        scan.updated_at = datetime.now(timezone.utc)
        scan_store.update(scan)

    return scan, resumed_objects