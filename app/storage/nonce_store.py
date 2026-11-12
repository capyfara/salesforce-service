import random
import psycopg2.pool
from contextlib import contextmanager

_SQL = """
CREATE TABLE IF NOT EXISTS hmac_nonces (
    key_id     TEXT NOT NULL,
    nonce      TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (key_id, nonce)
);
CREATE INDEX IF NOT EXISTS idx_hmac_nonces_expires ON hmac_nonces (expires_at);
"""

class PostgresNonceStore:
    def __init__(self, dsn: str, minconn: int = 1, maxconn: int = 3):
        self._pool = psycopg2.pool.ThreadedConnectionPool(minconn, maxconn, dsn)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(_SQL)
            conn.commit()

    @contextmanager
    def _conn(self):
        conn = self._pool.getconn()
        try:
            yield conn
        finally:
            self._pool.putconn(conn)

    def consume(self, key_id: str, nonce: str, ttl_seconds: int) -> bool:
        """True if the nonce was unused (now recorded); False if it's a replay."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO hmac_nonces (key_id, nonce, expires_at)
                   VALUES (%s, %s, now() + make_interval(secs => %s))
                   ON CONFLICT DO NOTHING""",
                (key_id, nonce, ttl_seconds),
            )
            fresh = cur.rowcount == 1
            if random.random() < 0.01:  # opportunistic purge
                cur.execute("DELETE FROM hmac_nonces WHERE expires_at < now()")
            conn.commit()
            return fresh