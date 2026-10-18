"""SQLite storage for temporary campaign data."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id                  TEXT PRIMARY KEY,
    status              TEXT NOT NULL,          -- pending_payment | paid | running | completed | stopped
    created_at          TEXT NOT NULL,
    paid_at             TEXT,
    expires_at          TEXT NOT NULL,
    payment_ref         TEXT,
    business_name       TEXT,
    business_description TEXT,
    product_details     TEXT,
    sales_instructions  TEXT,
    welcome_message     TEXT,
    language            TEXT,
    bolna_agent_id      TEXT,
    started_at          TEXT,
    finished_at         TEXT,
    error               TEXT
);

CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id     TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    row_number      INTEGER NOT NULL,
    name            TEXT NOT NULL,
    phone           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    execution_id    TEXT,
    outcome         TEXT,
    summary         TEXT,
    transcript      TEXT,
    duration_seconds REAL,
    recording_url   TEXT,
    extracted_data  TEXT,
    error           TEXT,
    called_at       TEXT,
    updated_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_leads_campaign ON leads(campaign_id);
CREATE INDEX IF NOT EXISTS idx_leads_execution ON leads(execution_id);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # --- campaigns -----------------------------------------------------------

    def create_campaign(self, campaign_id: str, expires_at: datetime) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO campaigns (id, status, created_at, expires_at) VALUES (?, 'pending_payment', ?, ?)",
                (campaign_id, iso(utcnow()), iso(expires_at)),
            )

    def get_campaign(self, campaign_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        return dict(row) if row else None

    def update_campaign(self, campaign_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE campaigns SET {cols} WHERE id = ?", (*fields.values(), campaign_id))

    def mark_paid(self, campaign_id: str, payment_ref: str, expires_at: datetime) -> bool:
        """Idempotently move a campaign from pending_payment to paid."""
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE campaigns SET status = 'paid', paid_at = ?, payment_ref = ?, expires_at = ? "
                "WHERE id = ? AND status = 'pending_payment'",
                (iso(utcnow()), payment_ref, iso(expires_at), campaign_id),
            )
            return cur.rowcount == 1

    def campaigns_with_status(self, status: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM campaigns WHERE status = ?", (status,)).fetchall()
        return [dict(r) for r in rows]

    def expired_campaigns(self, now: datetime) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM campaigns WHERE expires_at <= ?", (iso(now),)).fetchall()
        return [dict(r) for r in rows]

    def delete_campaign(self, campaign_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM leads WHERE campaign_id = ?", (campaign_id,))
            conn.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))

    # --- leads ---------------------------------------------------------------

    def replace_leads(self, campaign_id: str, leads: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM leads WHERE campaign_id = ?", (campaign_id,))
            conn.executemany(
                "INSERT INTO leads (campaign_id, row_number, name, phone, updated_at) VALUES (?, ?, ?, ?, ?)",
                [(campaign_id, l["row_number"], l["name"], l["phone"], iso(utcnow())) for l in leads],
            )

    def get_leads(self, campaign_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM leads WHERE campaign_id = ? ORDER BY row_number", (campaign_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def leads_with_status(self, campaign_id: str, statuses: tuple[str, ...], limit: int | None = None) -> list[dict]:
        marks = ",".join("?" for _ in statuses)
        sql = f"SELECT * FROM leads WHERE campaign_id = ? AND status IN ({marks}) ORDER BY row_number"
        params: list = [campaign_id, *statuses]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_lead_by_execution(self, execution_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM leads WHERE execution_id = ?", (execution_id,)).fetchone()
        return dict(row) if row else None

    def update_lead(self, lead_id: int, **fields) -> None:
        fields["updated_at"] = iso(utcnow())
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE leads SET {cols} WHERE id = ?", (*fields.values(), lead_id))

    def skip_pending_leads(self, campaign_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE leads SET status = 'skipped', updated_at = ? WHERE campaign_id = ? AND status = 'pending'",
                (iso(utcnow()), campaign_id),
            )
