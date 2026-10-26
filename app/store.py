"""Campaigns and their leads, saved in the database so a crash, restart or redeploy never loses a running
campaign.

Lead data is temporary: a campaign and all its leads (names, phone numbers, transcripts, results) are permanently
deleted RESULTS_RETENTION_DAYS after the campaign finishes (or after it was created, if it never started).
Only the campaign's cost totals are kept, in the usage table (accounts.py).
"""

import json
from datetime import datetime, timedelta

from .db import Database, iso, utcnow  # noqa: F401  (iso/utcnow re-exported for callers)

CAMPAIGN_COLUMNS = ("id", "user_id", "status", "created_at", "finished_at")
LEAD_COLUMNS = ("id", "campaign_id", "row_number", "status", "execution_id", "billed")

NEW_CAMPAIGN = {
    "business_name": None, "business_description": None, "product_details": None, "sales_instructions": None,
    "welcome_message": None, "language": None, "voice": None,
    "detail_fields": [],      # extra Excel columns: [{"key": "city", "label": "City"}, ...]
    "bolna_agent_id": None,
    "low_balance": False,     # calling paused until the wallet is topped up
    "started_at": None,
}
NEW_LEAD = {
    "outcome": None, "summary": None, "transcript": None, "duration_seconds": None, "recording_url": None,
    "extracted_data": None, "error": None, "called_at": None, "cost_paise": 0, "callback_time": None,
    "key_points": None,
}


def _campaign(row: dict | None) -> dict | None:
    if not row:
        return None
    campaign = {**NEW_CAMPAIGN, **json.loads(row["data"])}
    campaign.update({k: row[k] for k in CAMPAIGN_COLUMNS})
    return campaign


def _lead(row: dict | None) -> dict | None:
    if not row:
        return None
    lead = {**NEW_LEAD, **json.loads(row["data"])}
    lead.update({k: row[k] for k in LEAD_COLUMNS})
    lead["billed"] = bool(lead["billed"])
    return lead


def _split(fields: dict, columns: tuple[str, ...]) -> tuple[dict, dict]:
    cols = {k: v for k, v in fields.items() if k in columns}
    rest = {k: v for k, v in fields.items() if k not in columns}
    return cols, rest


class Store:
    def __init__(self, db: Database):
        self.db = db
        self._lock_clause = " FOR UPDATE" if db.postgres else ""

    # --- campaigns -----------------------------------------------------------

    def create_campaign(self, campaign_id: str, user_id: str, prefill: dict | None = None) -> None:
        data = {**NEW_CAMPAIGN, **(prefill or {})}
        with self.db.tx() as db:
            db.execute("INSERT INTO campaigns (id, user_id, status, created_at, data) VALUES (?, ?, 'setup', ?, ?)",
                       (campaign_id, user_id, utcnow().isoformat(), json.dumps(data)))

    def get_campaign(self, campaign_id: str) -> dict | None:
        with self.db.tx() as db:
            return _campaign(db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone())

    def update_campaign(self, campaign_id: str, **fields) -> None:
        cols, rest = _split(fields, CAMPAIGN_COLUMNS)
        with self.db.tx() as db:
            if rest:
                row = db.execute("SELECT data FROM campaigns WHERE id = ?" + self._lock_clause,
                                 (campaign_id,)).fetchone()
                if not row:
                    return
                cols["data"] = json.dumps({**json.loads(row["data"]), **rest})
            if cols:
                db.execute(f"UPDATE campaigns SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
                           (*cols.values(), campaign_id))

    def campaigns_for_user(self, user_id: str) -> list[dict]:
        with self.db.tx() as db:
            rows = db.execute("SELECT * FROM campaigns WHERE user_id = ? ORDER BY created_at DESC",
                              (user_id,)).fetchall()
        return [_campaign(r) for r in rows]

    def campaigns_with_status(self, *statuses: str) -> list[dict]:
        marks = ",".join("?" for _ in statuses)
        with self.db.tx() as db:
            rows = db.execute(f"SELECT * FROM campaigns WHERE status IN ({marks})", statuses).fetchall()
        return [_campaign(r) for r in rows]

    def active_campaigns(self) -> list[dict]:
        """Campaigns the worker must look after: calling, or stopped with calls still finishing."""
        with self.db.tx() as db:
            rows = db.execute("SELECT * FROM campaigns WHERE status = 'running' OR "
                              "(status = 'stopped' AND finished_at IS NULL)").fetchall()
        return [_campaign(r) for r in rows]

    def purge_expired(self, now: datetime, retention: timedelta) -> int:
        """Permanently delete campaigns (and their leads) past the retention period. Running ones are kept."""
        cutoff = iso(now - retention)
        with self.db.tx() as db:
            return db.execute("DELETE FROM campaigns WHERE (finished_at IS NOT NULL AND finished_at < ?) "
                              "OR (status = 'setup' AND created_at < ?)", (cutoff, cutoff)).rowcount

    # --- leads ---------------------------------------------------------------

    def replace_leads(self, campaign_id: str, leads: list[dict]) -> None:
        rows = [(campaign_id, lead["row_number"], "pending",
                 json.dumps({**NEW_LEAD, "name": lead["name"], "phone": lead["phone"],
                             "details": dict(lead.get("details") or {})}))
                for lead in leads]
        with self.db.tx() as db:
            db.execute("DELETE FROM leads WHERE campaign_id = ?", (campaign_id,))
            db.executemany("INSERT INTO leads (campaign_id, row_number, status, data) VALUES (?, ?, ?, ?)", rows)

    def get_leads(self, campaign_id: str) -> list[dict]:
        with self.db.tx() as db:
            rows = db.execute("SELECT * FROM leads WHERE campaign_id = ? ORDER BY row_number, id",
                              (campaign_id,)).fetchall()
        return [_lead(r) for r in rows]

    def leads_with_status(self, campaign_id: str, statuses: tuple[str, ...], limit: int | None = None) -> list[dict]:
        marks = ",".join("?" for _ in statuses)
        sql = f"SELECT * FROM leads WHERE campaign_id = ? AND status IN ({marks}) ORDER BY row_number, id"
        params: list = [campaign_id, *statuses]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.db.tx() as db:
            rows = db.execute(sql, params).fetchall()
        return [_lead(r) for r in rows]

    def get_lead_by_execution(self, execution_id: str) -> dict | None:
        with self.db.tx() as db:
            return _lead(db.execute("SELECT * FROM leads WHERE execution_id = ?", (execution_id,)).fetchone())

    def update_lead(self, lead_id: int, **fields) -> None:
        cols, rest = _split(fields, LEAD_COLUMNS)
        cols.pop("id", None)
        if "billed" in cols:
            cols["billed"] = int(bool(cols["billed"]))
        with self.db.tx() as db:
            if rest:
                row = db.execute("SELECT data FROM leads WHERE id = ?" + self._lock_clause, (lead_id,)).fetchone()
                if not row:
                    return
                cols["data"] = json.dumps({**json.loads(row["data"]), **rest}, default=str)
            if cols:
                db.execute(f"UPDATE leads SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
                           (*cols.values(), lead_id))

    def claim_billing(self, lead_id: int) -> bool:
        """Mark a lead's call as billed; True only the first time (webhook and polling may both see it)."""
        with self.db.tx() as db:
            return db.execute("UPDATE leads SET billed = 1 WHERE id = ? AND billed = 0", (lead_id,)).rowcount == 1

    def skip_pending_leads(self, campaign_id: str) -> None:
        with self.db.tx() as db:
            db.execute("UPDATE leads SET status = 'skipped' WHERE campaign_id = ? AND status = 'pending'",
                       (campaign_id,))
