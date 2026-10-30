"""Database access for accounts, wallets, campaigns and leads.

Production uses PostgreSQL (Supabase) when DATABASE_URL is set; local runs and tests use a SQLite file.
SQL is written once with "?" placeholders and runs on both. On PostgreSQL every table lives in the private
"intuvora" schema, which Supabase's public REST API does not expose.
"""

import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

PG_SCHEMA = "intuvora"
IDLE_CHECK_AFTER = 30  # seconds; a pooled connection idle longer than this is tested before use

# {ID} becomes an auto-increment primary key for the dialect in use.
TABLES = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    auth_id       TEXT,                              -- Supabase Auth user id
    balance_paise BIGINT NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topups (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount_paise BIGINT NOT NULL,
    payment_ref  TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',     -- pending | credited
    created_at   TEXT NOT NULL,
    credited_at  TEXT
);
CREATE TABLE IF NOT EXISTS wallet_transactions (     -- every credit and debit, for audits and disputes
    id                  {ID},
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL,                -- topup | call
    amount_paise        BIGINT NOT NULL,              -- + credit, - debit
    balance_after_paise BIGINT NOT NULL,
    reference           TEXT,                         -- top-up id or campaign id
    created_at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (                     -- per-campaign totals, kept after lead data is deleted
    campaign_id   TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    business_name TEXT,
    started_at    TEXT NOT NULL,
    calls         INTEGER NOT NULL DEFAULT 0,
    seconds       INTEGER NOT NULL DEFAULT 0,
    amount_paise  BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS profiles (                  -- last-used business details, for auto-fill
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaigns (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status      TEXT NOT NULL,                        -- setup | running | completed | stopped
    created_at  TEXT NOT NULL,
    finished_at TEXT,
    data        TEXT NOT NULL                         -- business details, settings (JSON)
);
CREATE TABLE IF NOT EXISTS leads (                     -- temporary: deleted with the campaign after retention
    id           {ID},
    campaign_id  TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    row_number   INTEGER NOT NULL,
    status       TEXT NOT NULL,
    execution_id TEXT,
    billed       INTEGER NOT NULL DEFAULT 0,
    data         TEXT NOT NULL                         -- name, phone, details, call result (JSON)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_auth ON users(auth_id);
CREATE INDEX IF NOT EXISTS idx_topups_user ON topups(user_id);
CREATE INDEX IF NOT EXISTS idx_tx_user ON wallet_transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage(user_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_user ON campaigns(user_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);
CREATE INDEX IF NOT EXISTS idx_leads_campaign ON leads(campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_leads_execution ON leads(execution_id);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class Result:
    """Cursor wrapper: rows come back as plain dicts on both databases."""

    def __init__(self, cursor):
        self._cursor = cursor
        self.rowcount = cursor.rowcount

    def fetchone(self) -> dict | None:
        row = self._cursor.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self) -> list[dict]:
        return [dict(r) for r in self._cursor.fetchall()]


class Tx:
    def __init__(self, conn, postgres: bool):
        self._conn = conn
        self._postgres = postgres

    def execute(self, sql: str, params: tuple | list = ()) -> Result:
        if self._postgres:
            sql = sql.replace("?", "%s")
        cursor = self._conn.cursor()
        cursor.execute(sql, params)
        return Result(cursor)

    def executemany(self, sql: str, rows: list) -> None:
        if not rows:
            return
        if self._postgres:
            sql = sql.replace("?", "%s")
        self._conn.cursor().executemany(sql, rows)


class Database:
    def __init__(self, url: str):
        """url: a postgresql:// connection string (Supabase), or a SQLite file path."""
        self.postgres = url.startswith(("postgres://", "postgresql://"))
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            with psycopg.connect(url, autocommit=True, prepare_threshold=None) as conn:  # once, before the pool
                conn.execute(f"CREATE SCHEMA IF NOT EXISTS {PG_SCHEMA}")

            def configure(conn):
                conn.execute(f"SET search_path TO {PG_SCHEMA}")
                conn.last_used = time.monotonic()

            def returned(conn):
                conn.last_used = time.monotonic()

            def check(conn):
                # Supabase drops connections left idle, so test those; skip the extra round trip for busy ones.
                if time.monotonic() - getattr(conn, "last_used", 0) > IDLE_CHECK_AFTER:
                    ConnectionPool.check_connection(conn)

            # autocommit: a single statement needs no BEGIN/COMMIT round trips; tx(atomic=True) adds them.
            # min_size 3: a page's parallel requests don't each wait for a new connection to Supabase.
            self._pool = ConnectionPool(url, min_size=3, max_size=6, open=True, configure=configure,
                                        check=check, reset=returned,
                                        kwargs={"row_factory": dict_row, "prepare_threshold": None,
                                                "autocommit": True})
        else:
            self.path = url
            if url != ":memory:":
                Path(url).parent.mkdir(parents=True, exist_ok=True)
            self._lock = threading.RLock()  # SQLite allows one writer; serialise to avoid "database is locked"
        self._create_tables()

    def _create_tables(self) -> None:
        ddl = TABLES.replace("{ID}", "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY" if self.postgres
                             else "INTEGER PRIMARY KEY AUTOINCREMENT")
        with self.tx(atomic=True) as db:
            if not self.postgres:  # local databases made by earlier versions: add the new column first
                columns = {r["name"] for r in db.execute("PRAGMA table_info(users)").fetchall()}
                if columns and "auth_id" not in columns:
                    db.execute("ALTER TABLE users ADD COLUMN auth_id TEXT")
                self.legacy_password_column = "password_hash" in columns
            else:
                self.legacy_password_column = False
            for statement in (s.strip() for s in ddl.split(";")):
                if statement:
                    db.execute(statement)

    @contextmanager
    def tx(self, atomic: bool = False):
        """A database session. atomic=True: one transaction, committed when the block ends, rolled back on error;
        needed when several statements must all happen or none (money moves, read-then-write).
        Otherwise each statement on PostgreSQL commits on its own, saving two round trips."""
        if self.postgres:
            with self._pool.connection() as conn:
                if atomic:
                    with conn.transaction():
                        yield Tx(conn, True)
                else:
                    yield Tx(conn, True)
            return
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            try:
                yield Tx(conn, False)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def close(self) -> None:
        if self.postgres:
            self._pool.close()
