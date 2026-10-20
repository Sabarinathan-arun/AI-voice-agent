"""In-memory session store. Nothing is written to disk: a campaign lives only while the server runs.

Finished sessions are dropped from memory after a while so the server does not grow forever.
"""

import copy
import threading
from datetime import datetime, timedelta, timezone

IDLE_SESSION_LIFETIME = timedelta(hours=24)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class Store:
    def __init__(self):
        self._lock = threading.Lock()
        self._campaigns: dict[str, dict] = {}
        self._leads: dict[str, list[dict]] = {}
        self._next_lead_id = 1

    def _touch(self, campaign: dict) -> None:
        campaign["last_activity"] = utcnow()

    # --- campaigns -----------------------------------------------------------

    def create_campaign(self, campaign_id: str) -> None:
        with self._lock:
            self._campaigns[campaign_id] = {
                "id": campaign_id,
                "status": "pending_payment",  # pending_payment | paid | running | completed | stopped
                "created_at": iso(utcnow()),
                "paid_at": None,
                "payment_ref": None,
                "business_name": None,
                "business_description": None,
                "product_details": None,
                "sales_instructions": None,
                "welcome_message": None,
                "language": None,
                "detail_fields": [],
                "bolna_agent_id": None,  # extra Excel columns: [{"key": "city", "label": "City"}, ...]
                "started_at": None,
                "finished_at": None,
                "last_activity": utcnow(),
            }
            self._leads[campaign_id] = []

    def get_campaign(self, campaign_id: str) -> dict | None:
        with self._lock:
            campaign = self._campaigns.get(campaign_id)
            if campaign:
                self._touch(campaign)
            return copy.deepcopy(campaign)

    def update_campaign(self, campaign_id: str, **fields) -> None:
        with self._lock:
            campaign = self._campaigns.get(campaign_id)
            if campaign:
                campaign.update(fields)
                self._touch(campaign)

    def mark_paid(self, campaign_id: str) -> bool:
        with self._lock:
            campaign = self._campaigns.get(campaign_id)
            if not campaign or campaign["status"] != "pending_payment":
                return False
            campaign.update(status="paid", paid_at=iso(utcnow()))
            return True

    def campaigns_with_status(self, *statuses: str) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(c) for c in self._campaigns.values() if c["status"] in statuses]

    def drop_idle(self, now: datetime) -> list[dict]:
        """Forget sessions untouched for IDLE_SESSION_LIFETIME (never one that is still calling)."""
        with self._lock:
            idle = [c for c in self._campaigns.values()
                    if c["status"] != "running" and now - c["last_activity"] > IDLE_SESSION_LIFETIME]
            for c in idle:
                del self._campaigns[c["id"]]
                del self._leads[c["id"]]
            return idle

    # --- leads ---------------------------------------------------------------

    def replace_leads(self, campaign_id: str, leads: list[dict]) -> None:
        with self._lock:
            rows = []
            for lead in leads:
                rows.append({
                    "id": self._next_lead_id, "campaign_id": campaign_id,
                    "row_number": lead["row_number"], "name": lead["name"], "phone": lead["phone"],
                    "details": dict(lead.get("details") or {}),
                    "status": "pending", "execution_id": None, "outcome": None, "summary": None,
                    "transcript": None, "duration_seconds": None, "recording_url": None,
                    "extracted_data": None, "error": None, "called_at": None,
                })
                self._next_lead_id += 1
            self._leads[campaign_id] = rows

    def get_leads(self, campaign_id: str) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._leads.get(campaign_id, []))

    def leads_with_status(self, campaign_id: str, statuses: tuple[str, ...], limit: int | None = None) -> list[dict]:
        with self._lock:
            found = [l for l in self._leads.get(campaign_id, []) if l["status"] in statuses]
            return copy.deepcopy(found[:limit] if limit is not None else found)

    def get_lead_by_execution(self, execution_id: str) -> dict | None:
        with self._lock:
            for leads in self._leads.values():
                for lead in leads:
                    if lead["execution_id"] == execution_id:
                        return copy.deepcopy(lead)
            return None

    def update_lead(self, lead_id: int, **fields) -> None:
        with self._lock:
            for leads in self._leads.values():
                for lead in leads:
                    if lead["id"] == lead_id:
                        lead.update(fields)
                        return

    def skip_pending_leads(self, campaign_id: str) -> None:
        with self._lock:
            for lead in self._leads.get(campaign_id, []):
                if lead["status"] == "pending":
                    lead["status"] = "skipped"
