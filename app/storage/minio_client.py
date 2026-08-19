import io
import logging
import pandas
from minio import Minio
from minio.error import S3Error

logger = logging.getLogger(__name__)

class MinioStorageClient:
    def __init__(self, endpoint:str, access_key:str, secret_key:str, secure:bool, bucket:str):
        self._client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self._bucket = bucket
        self.ensure_bucket()

    def ensure_bucket(self):
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)
            logger.info(f"Created MinIO bucket:  {self._bucket}")

    def check_bucket(self):
        return self._client.bucket_exists(self._bucket)
    
    def upload_records(self, records: list[dict], org_id:str, scan_id:str, object_name: str, page_num: int) -> str:
        """Converts a page of records to Parquet and uploads. Returns the s3:// path."""
        if not records:
            logger.info(f"No records to upload for {object_name} page {page_num}")
            return ""
        
        df = pandas.DataFrame(records)
        buffer = io.BytesIO()
        df.to_parquet(buffer, engine="pyarrow", index=False)
        buffer.seek(0)

        # Matches the path shape shown in Section 5.3's minio_path field
        object_key = f"{org_id}/{scan_id}/{object_name.lower()}/page-{page_num:04d}.parquet"

        self._client.put_object(
            bucket_name=self._bucket,
            object_name=object_key,
            data=buffer,
            length=buffer.getbuffer().nbytes,
            content_type="application/octet-stream",
        )

        path = f"s3://{self._bucket}/{object_key}"
        logger.info(f"Uploaded {len(records)} records to {path}")
        return path