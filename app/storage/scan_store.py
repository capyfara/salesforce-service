"""
Postgres-backed replacement for InMemoryScanStore.

Storage model: one row per scan, full ScanState serialized to a JSONB column.

Locking: Postgres advisory locks (pg_advisory_lock(hashtext(scan_id)))
so the lock is honored across multiple worker processes / Nomad
allocations, not just within one Python process.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager

import psycopg2
import psycopg2.pool
import psycopg2.extras

from models.scan import ScanState

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scans (
    scan_id     TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL,
    status      TEXT NOT NULL,
    data        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_scans_org_id ON scans (org_id);
CREATE INDEX IF NOT EXISTS idx_scans_status ON scans (status);
"""


class PostgresScanStore:
    def __init__(self, dsn: str, minconn: int = 1, maxconn: int = 10):
        """
        dsn example:
          f"host={settings.DB_HOST} port={settings.DB_PORT} dbname={settings.DB_NAME} "
          f"user={settings.DB_USER} password={settings.DB_PASSWORD} "
          f"options=-csearch_path={settings.DB_SCHEMA}"
        """
        self._pool = psycopg2.pool.ThreadedConnectionPool(minconn, maxconn, dsn)
        self._ensure_schema()

    # Connection Helpers

    @contextmanager
    def _conn(self):
        conn = self._pool.getconn()
        try:
            yield conn
        finally:
            self._pool.putconn(conn)

    def _ensure_schema(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(_SCHEMA_SQL)
            conn.commit()

    @staticmethod
    def _to_json(scan: ScanState) -> str:
        # model_dump_json() handles datetimes/enums the way ScanState defines them
        return scan.model_dump_json()

    @staticmethod
    def _from_row(row: dict) -> ScanState:
        return ScanState.model_validate(row["data"])

    # Public Interface
    
    def create(self, scan: ScanState) -> None:
        self.update(scan)

    def get(self, scan_id: str) -> ScanState | None:
        with self._conn() as conn, conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute("SELECT data FROM scans WHERE scan_id = %s", (scan_id,))
            row = cur.fetchone()
            return self._from_row(row) if row else None

    def update(self, scan: ScanState) -> None:
        payload = self._to_json(scan)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scans (scan_id, org_id, status, data, updated_at)
                VALUES (%s, %s, %s, %s::jsonb, now())
                ON CONFLICT (scan_id) DO UPDATE
                SET org_id     = EXCLUDED.org_id,
                    status     = EXCLUDED.status,
                    data       = EXCLUDED.data,
                    updated_at = now()
                """,
                (scan.scan_id, scan.org_id, scan.status, payload),
            )
            conn.commit()

    def list_all(self) -> list[ScanState]:
        with self._conn() as conn, conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute("SELECT data FROM scans ORDER BY created_at DESC")
            return [self._from_row(r) for r in cur.fetchall()]

    def delete(self, scan_id: str) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM scans WHERE scan_id = %s", (scan_id,))
            conn.commit()

    @contextmanager
    def lock(self, scan_id: str):
        """
        Cross-process lock using a Postgres session-level advisory lock, keyed by
        hashtext(scan_id). Held for the lifetime of one dedicated connection
        checked out from the pool, then released and returned to the pool.
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (scan_id,))
            conn.commit()
            try:
                yield
            finally:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (scan_id,))
                conn.commit()
        finally:
            self._pool.putconn(conn)