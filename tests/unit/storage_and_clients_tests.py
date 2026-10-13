"""Unit tests for MinioStorageClient, CoreServiceClient and PostgresScanStore.
All external systems (MinIO, HTTP, Postgres) are replaced with mocks."""
import hashlib
import hmac as hmaclib
import io
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas
import pytest

import clients.core_service_client as core_mod
import storage.minio_client as minio_mod
import storage.scan_store as store_mod
from auth.hmac_auth import verify_hmac
from clients.core_service_client import CoreServiceClient
from models.scan import ObjectProgress, ScanState, ScanStatus
from storage.minio_client import MinioStorageClient
from storage.scan_store import PostgresScanStore


# ===========================================================================
# MinioStorageClient
# ===========================================================================
@pytest.fixture
def minio_sdk(monkeypatch):
    sdk = MagicMock()
    sdk.bucket_exists.return_value = True
    monkeypatch.setattr(minio_mod, "Minio", MagicMock(return_value=sdk))
    return sdk


def make_minio(**kw):
    return MinioStorageClient("minio:9000", "ak", "sk", False, kw.get("bucket", "bkt"))


def test_minio_constructor_wires_credentials(monkeypatch, minio_sdk):
    ctor = MagicMock(return_value=minio_sdk)
    monkeypatch.setattr(minio_mod, "Minio", ctor)
    MinioStorageClient("h:9000", "ak", "sk", True, "b")
    ctor.assert_called_once_with("h:9000", access_key="ak", secret_key="sk", secure=True)


def test_minio_creates_missing_bucket_on_init(minio_sdk):
    minio_sdk.bucket_exists.return_value = False
    make_minio(bucket="new")
    minio_sdk.make_bucket.assert_called_once_with("new")


def test_minio_leaves_existing_bucket_alone(minio_sdk):
    make_minio()
    minio_sdk.make_bucket.assert_not_called()


def test_minio_check_bucket_delegates(minio_sdk):
    c = make_minio()
    minio_sdk.bucket_exists.return_value = False
    assert c.check_bucket() is False


def test_minio_upload_with_no_records_is_a_noop(minio_sdk):
    assert make_minio().upload_records([], "o", "s", "Contact", 1) == ""
    minio_sdk.put_object.assert_not_called()


def test_minio_upload_returns_s3_path_and_uses_padded_lowercase_key(minio_sdk):
    path = make_minio().upload_records([{"Id": "1"}], "org-1", "scan-1", "Contact", 7)
    assert path == "s3://bkt/org-1/scan-1/contact/page_0007.parquet"
    kw = minio_sdk.put_object.call_args.kwargs
    assert kw["bucket_name"] == "bkt"
    assert kw["object_name"] == "org-1/scan-1/contact/page_0007.parquet"


def test_minio_uploaded_bytes_are_valid_parquet_matching_input(minio_sdk):
    records = [{"Id": "1", "Name": "A"}, {"Id": "2", "Name": None}]
    make_minio().upload_records(records, "o", "s", "Account", 1)
    kw = minio_sdk.put_object.call_args.kwargs
    raw = kw["data"].read()
    assert kw["length"] == len(raw)
    df = pandas.read_parquet(io.BytesIO(raw))
    assert list(df["Id"]) == ["1", "2"]
    assert list(df.columns) == ["Id", "Name"]


def test_minio_upload_propagates_storage_errors(minio_sdk):
    minio_sdk.put_object.side_effect = ConnectionError("down")
    with pytest.raises(ConnectionError):
        make_minio().upload_records([{"Id": "1"}], "o", "s", "Contact", 1)


# ===========================================================================
# CoreServiceClient
# ===========================================================================
KEY = "core-key-" + "x" * 32


@pytest.fixture
def http(monkeypatch):
    patch = MagicMock()
    monkeypatch.setattr(core_mod.requests, "patch", patch)
    return patch


def test_core_report_patches_scan_url_with_json_body(http):
    CoreServiceClient("http://core:5700", KEY).report_scan_status("scan-1", "complete", 42)
    args, kw = http.call_args
    assert args[0] == "http://core:5700/scans/scan-1"
    assert json.loads(kw["data"]) == {"status": "complete", "records": 42}
    assert kw["headers"]["Content-Type"] == "application/json"
    assert kw["timeout"] == 30


def test_core_report_serialises_str_enum_status(http):
    CoreServiceClient("http://c", KEY).report_scan_status("s", ScanStatus.COMPLETE, 1)
    assert json.loads(http.call_args.kwargs["data"])["status"] == "complete"


def test_core_signature_is_hmac_sha256_of_timestamp_dot_body(http):
    CoreServiceClient("http://c", KEY).report_scan_status("s", "complete", 1)
    h, body = http.call_args.kwargs["headers"], http.call_args.kwargs["data"]
    expected = hmaclib.new(KEY.encode(), f"{h['X-Timestamp']}.{body}".encode(), hashlib.sha256).hexdigest()
    assert h["X-Signature"] == expected
    assert h["X-Key-Id"] == "core"
    assert abs(float(h["X-Timestamp"]) - time.time()) < 5


def test_core_signature_is_accepted_by_this_services_verifier(http):
    """Round-trip: what CoreServiceClient signs, verify_hmac accepts (same scheme both ways)."""
    CoreServiceClient("http://c", KEY).report_scan_status("s", "complete", 1)
    h, body = http.call_args.kwargs["headers"], http.call_args.kwargs["data"]
    settings = SimpleNamespace(HMAC_ENABLED=True, HMAC_SECRET_KEY_CORE=KEY,
                               HMAC_SECRET_KEY_ENGINEER="e", HMAC_SIGNATURE_MAX_AGE=300)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)),
                          state=SimpleNamespace(raw_body=body.encode()))
    verify_hmac(req, x_signature=h["X-Signature"], x_timestamp=h["X-Timestamp"], x_key_id=h["X-Key-Id"])


def test_core_signature_changes_with_body(http):
    c = CoreServiceClient("http://c", KEY)
    c.report_scan_status("s", "complete", 1)
    c.report_scan_status("s", "complete", 2)
    sigs = [call.kwargs["headers"]["X-Signature"] for call in http.call_args_list]
    assert sigs[0] != sigs[1]


def test_core_http_errors_propagate(http):
    http.return_value.raise_for_status.side_effect = RuntimeError("502")
    with pytest.raises(RuntimeError):
        CoreServiceClient("http://c", KEY).report_scan_status("s", "failed", 0)


# ===========================================================================
# PostgresScanStore (psycopg2 pool mocked)
# ===========================================================================
@pytest.fixture
def pg(monkeypatch):
    cur = MagicMock(name="cursor")
    conn = MagicMock(name="conn")
    conn.cursor.return_value.__enter__.return_value = cur
    pool = MagicMock(name="pool")
    pool.getconn.return_value = conn
    monkeypatch.setattr(store_mod.psycopg2.pool, "ThreadedConnectionPool", MagicMock(return_value=pool))
    return SimpleNamespace(cur=cur, conn=conn, pool=pool)


def sql_of(call):
    return " ".join(call.args[0].split())


def test_store_creates_schema_on_init(pg):
    PostgresScanStore("dsn")
    ddl = sql_of(pg.cur.execute.call_args_list[0])
    assert "CREATE TABLE IF NOT EXISTS scans" in ddl and "idx_scans_org_id" in ddl and "idx_scans_status" in ddl
    pg.conn.commit.assert_called()


def test_store_pool_sizing(monkeypatch, pg):
    ctor = store_mod.psycopg2.pool.ThreadedConnectionPool
    PostgresScanStore("dsn", minconn=2, maxconn=4)
    ctor.assert_called_once_with(2, 4, "dsn")


def test_store_returns_connection_to_pool_even_on_error(pg):
    s = PostgresScanStore("dsn")
    pg.pool.putconn.reset_mock()
    pg.cur.execute.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        s.delete("x")
    pg.pool.putconn.assert_called_once_with(pg.conn)


def test_store_update_is_an_upsert_with_expected_params(pg):
    s = PostgresScanStore("dsn")
    scan = ScanState(scan_id="s1", org_id="o1", status=ScanStatus.FAILED)
    s.update(scan)
    call = pg.cur.execute.call_args_list[-1]
    assert "ON CONFLICT (scan_id) DO UPDATE" in sql_of(call)
    scan_id, org_id, status, payload = call.args[1]
    assert (scan_id, org_id, status) == ("s1", "o1", ScanStatus.FAILED)
    assert json.loads(payload)["scan_id"] == "s1"
    pg.conn.commit.assert_called()


def test_store_create_delegates_to_update(pg):
    s = PostgresScanStore("dsn")
    s.create(ScanState(scan_id="s1", org_id="o"))
    assert "ON CONFLICT" in sql_of(pg.cur.execute.call_args_list[-1])


def test_store_get_missing_returns_none(pg):
    pg.cur.fetchone.return_value = None
    assert PostgresScanStore("dsn").get("nope") is None


def test_store_get_rehydrates_scan_state(pg):
    original = ScanState(scan_id="s1", org_id="o", progress={"Contact": ObjectProgress(sf_job_id="j")})
    pg.cur.fetchone.return_value = {"data": json.loads(original.model_dump_json())}
    got = PostgresScanStore("dsn").get("s1")
    assert got == original
    assert pg.cur.execute.call_args.args[1] == ("s1",)


def test_store_list_all_returns_scan_states_newest_first_query(pg):
    a, b = ScanState(scan_id="a", org_id="o"), ScanState(scan_id="b", org_id="o")
    pg.cur.fetchall.return_value = [{"data": json.loads(x.model_dump_json())} for x in (a, b)]
    s = PostgresScanStore("dsn")
    assert [x.scan_id for x in s.list_all()] == ["a", "b"]
    assert "ORDER BY created_at DESC" in sql_of(pg.cur.execute.call_args)


def test_store_delete_issues_delete_and_commits(pg):
    s = PostgresScanStore("dsn")
    s.delete("s1")
    call = pg.cur.execute.call_args_list[-1]
    assert sql_of(call).startswith("DELETE FROM scans") and call.args[1] == ("s1",)
    pg.conn.commit.assert_called()


def test_store_lock_acquires_then_releases_advisory_lock(pg):
    s = PostgresScanStore("dsn")
    pg.cur.execute.reset_mock()
    with s.lock("s1"):
        assert "pg_advisory_lock(hashtext" in sql_of(pg.cur.execute.call_args_list[-1])
    sqls = [sql_of(c) for c in pg.cur.execute.call_args_list]
    assert "pg_advisory_lock" in sqls[0] and "pg_advisory_unlock" in sqls[1]
    assert all(c.args[1] == ("s1",) for c in pg.cur.execute.call_args_list)


def test_store_lock_releases_when_body_raises(pg):
    s = PostgresScanStore("dsn")
    pg.cur.execute.reset_mock(); pg.pool.putconn.reset_mock()
    with pytest.raises(ValueError):
        with s.lock("s1"):
            raise ValueError("inside")
    assert "pg_advisory_unlock" in sql_of(pg.cur.execute.call_args_list[-1])
    pg.pool.putconn.assert_called_once_with(pg.conn)


def test_store_lock_returns_connection_if_acquire_fails(pg):
    s = PostgresScanStore("dsn")
    pg.pool.putconn.reset_mock()
    pg.cur.execute.side_effect = RuntimeError("cannot lock")
    with pytest.raises(RuntimeError):
        with s.lock("s1"):
            pass
    pg.pool.putconn.assert_called_once_with(pg.conn)


def test_store_lock_uses_a_dedicated_connection_held_for_the_whole_block(pg):
    s = PostgresScanStore("dsn")
    pg.pool.getconn.reset_mock(); pg.pool.putconn.reset_mock()
    with s.lock("s1"):
        pg.pool.putconn.assert_not_called()
    assert pg.pool.getconn.call_count == 1