import logging
import clickhouse_connect
from storage.clickhouse_schemas import SCHEMAS

logger = logging.getLogger(__name__)

class ClickHouseClient:
    def __init__(self, host, port, user, password, database, secure,
                 s3_base_url: str, s3_bucket: str, s3_access_key: str, s3_secret_key: str):
        self._conn_args = dict(host=host, port=port, username=user,
                               password=password, secure=secure)
        self._db = database
        self._s3_base = s3_base_url.rstrip("/")
        self._bucket = s3_bucket
        self._ak, self._sk = s3_access_key, s3_secret_key

    def _new_client(self):
        # clickhouse-connect clients can't run concurrent queries in one session,
        # and background scans run in a threadpool -> one client per operation.
        return clickhouse_connect.get_client(**self._conn_args)

    def ensure_schema(self) -> None:
        c = self._new_client()
        try:
            c.command(f"CREATE DATABASE IF NOT EXISTS {self._db}")
            for spec in SCHEMAS.values():
                c.command(spec.ddl(self._db))
        finally:
            c.close()

    def check(self) -> bool:
        c = self._new_client()
        try:
            return c.command("SELECT 1") == 1
        finally:
            c.close()

    def load_object(self, obj_name: str, org_id: str, scan_id: str) -> int:
        """Loads every page_*.parquet for one object/scan. Idempotent (ReplacingMergeTree)."""
        spec = SCHEMAS[obj_name]
        url = f"{self._s3_base}/{self._bucket}/{org_id}/{scan_id}/{obj_name.lower()}/page_*.parquet"
        c = self._new_client()
        try:
            c.command(spec.insert_sql(self._db), parameters={
                "org_id": org_id, "scan_id": scan_id, "url": url,
                "access_key": self._ak, "secret_key": self._sk,
            })
            return int(c.command(
                f"SELECT count() FROM {self._db}.{spec.table} "
                "WHERE org_id = %(o)s AND scan_id = %(s)s",
                parameters={"o": org_id, "s": scan_id},
            ))
        finally:
            c.close()