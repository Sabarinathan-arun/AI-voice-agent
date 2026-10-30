"""Campaigns and their leads, saved in the database so a crash, restart or redeploy never loses a running
campaign.

Lead data is temporary: a campaign and all its leads (names, phone numbers, transcripts, results) are permanently
deleted RESULTS_RETENTION_DAYS after the campaign finishes (or after it was created, if it never started).
Only the campaign's cost totals are kept, in the usage table (accounts.py).
"""

import json
import re
from datetime import datetime, timedelta

from .db import Database, iso, utcnow  # noqa: F401  (iso/utcnow re-exported for callers)

CAMPAIGN_COLUMNS = ("id", "user_id", "status", "created_at", "finished_at")
LEAD_COLUMNS = ("id", "campaign_id", "row_number", "status", "execution_id", "billed")

# Call statuses while a call is being placed or is live; a lead is finished once it is out of these and "pending".
IN_FLIGHT = ("dialing", "queued", "initiated", "ringing", "in-progress", "call-disconnected")
UNFINISHED_LEAD_STATUSES = ("pending", *IN_FLIGHT)

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

    def transition_campaign(self, campaign_id: str, from_status: str, to_status: str) -> bool:
        """Change the status only if it is still from_status; False if another request got there first."""
        with self.db.tx() as db:
            return db.execute("UPDATE campaigns SET status = ? WHERE id = ? AND status = ?",
                              (to_status, campaign_id, from_status)).rowcount == 1

    def finish_campaign(self, campaign_id: str, finished_at: str) -> bool:
        """Mark a campaign finished once: running becomes completed, stopped stays stopped."""
        with self.db.tx() as db:
            return db.execute("UPDATE campaigns SET finished_at = ?, status = CASE WHEN status = 'running' "
                              "THEN 'completed' ELSE status END WHERE id = ? AND finished_at IS NULL",
                              (finished_at, campaign_id)).rowcount == 1

    def campaign_summaries(self, user_id: str) -> list[dict]:
        """The user's campaigns, newest first, with lead counts, in one round trip (for the dashboard list)."""
        busy = ",".join("?" for _ in UNFINISHED_LEAD_STATUSES)
        with self.db.tx() as db:
            rows = db.execute(
                "SELECT c.*, COUNT(l.id) AS lead_count, "
                f"COALESCE(SUM(CASE WHEN l.id IS NOT NULL AND l.status NOT IN ({busy}) THEN 1 ELSE 0 END), 0) AS done_count "
                "FROM campaigns c LEFT JOIN leads l ON l.campaign_id = c.id WHERE c.user_id = ? "
                "GROUP BY c.id, c.user_id, c.status, c.created_at, c.finished_at, c.data ORDER BY c.created_at DESC",
                (*UNFINISHED_LEAD_STATUSES, user_id)).fetchall()
        return [{**_campaign(r), "lead_count": int(r["lead_count"]), "done_count": int(r["done_count"])}
                for r in rows]

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
                              "OR (status IN ('setup', 'starting') AND created_at < ?)", (cutoff, cutoff)).rowcount

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

    def claim_dial(self, lead_id: int) -> bool:
        """Move a lead from pending to dialing; False if it was skipped (campaign stopped) or already taken."""
        with self.db.tx() as db:
            return db.execute("UPDATE leads SET status = 'dialing' WHERE id = ? AND status = 'pending'",
                              (lead_id,)).rowcount == 1

    def find_lost_lead(self, agent_id: str, phone: str) -> dict | None:
        """A lead whose call Bolna placed but whose execution id was never saved (the server stopped mid-dial)."""
        digits = re.sub(r"\D", "", phone or "")
        if not (agent_id and digits):
            return None
        with self.db.tx() as db:
            rows = db.execute("SELECT l.*, c.data AS campaign_data FROM leads l JOIN campaigns c ON c.id = l.campaign_id "
                              "WHERE l.execution_id IS NULL AND l.status IN ('dialing', 'timed_out')").fetchall()
        for row in rows:
            if json.loads(row.pop("campaign_data")).get("bolna_agent_id") != agent_id:
                continue
            lead = _lead(row)
            if re.sub(r"\D", "", lead.get("phone") or "") == digits:
                return lead
        return None

    def claim_billing(self, lead_id: int) -> bool:
        """Mark a lead's call as billed; True only the first time (webhook and polling may both see it)."""
        with self.db.tx() as db:
            return db.execute("UPDATE leads SET billed = 1 WHERE id = ? AND billed = 0", (lead_id,)).rowcount == 1

    def skip_pending_leads(self, campaign_id: str) -> None:
        with self.db.tx() as db:
            db.execute("UPDATE leads SET status = 'skipped' WHERE campaign_id = ? AND status = 'pending'",
                       (campaign_id,))
