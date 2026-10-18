"""Background worker: places calls, collects results and deletes expired campaign data."""

import json
import logging
import threading
from datetime import datetime, timedelta

from .bolna import TERMINAL_STATUSES, BolnaError
from .db import Database, iso, utcnow

log = logging.getLogger(__name__)

IN_FLIGHT = ("dialing", "queued", "initiated", "ringing", "in-progress", "call-disconnected")
NOT_REACHED = {"no-answer": "no_answer", "busy": "busy", "failed": "failed", "error": "failed",
               "canceled": "cancelled", "cancelled": "cancelled", "stopped": "cancelled",
               "balance-low": "failed"}


def _first(data: dict, *keys):
    for key in keys:
        value = data.get(key)
        if value not in (None, "", {}, []):
            return value
    return None


def parse_execution(execution: dict) -> dict:
    """Map a Bolna execution payload to lead fields. Tolerant of missing fields."""
    status = str(execution.get("status") or "unknown").lower()
    telephony = execution.get("telephony_data") or {}
    extracted = execution.get("extracted_data")
    if isinstance(extracted, str):
        try:
            extracted = json.loads(extracted)
        except ValueError:
            extracted = {"raw": extracted}
    extracted = extracted if isinstance(extracted, dict) else {}

    transcript = _first(execution, "transcript")
    if isinstance(transcript, list):
        transcript = "\n".join(f"{t.get('role', '')}: {t.get('content', '')}" for t in transcript if isinstance(t, dict))
    summary = _first(execution, "summary")
    duration = _first(execution, "conversation_duration", "conversation_time") or _first(telephony, "duration")
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None

    if status not in TERMINAL_STATUSES and status not in IN_FLIGHT:
        status = "in-progress"  # unrecognised intermediate status; the call timeout still applies
    fields = {
        "status": status,
        "transcript": transcript,
        "summary": summary,
        "duration_seconds": duration,
        "recording_url": _first(telephony, "recording_url"),
        "extracted_data": json.dumps(extracted) if extracted else None,
    }
    if status in TERMINAL_STATUSES:
        fields["outcome"] = _outcome(status, extracted, transcript)
        if not summary and transcript:
            fields["summary"] = transcript[:300]
        hangup = _first(telephony, "hangup_reason")
        if status not in ("completed",) and hangup:
            fields["error"] = str(hangup)
    return fields


def _outcome(status: str, extracted: dict, transcript: str | None) -> str:
    if status in NOT_REACHED:
        return NOT_REACHED[status]
    value = extracted.get("outcome")
    if isinstance(value, dict):  # some extraction formats nest the value
        value = value.get("value") or value.get("outcome")
    if value:
        return str(value).strip().lower().replace(" ", "_")
    if not transcript or "user:" not in transcript.lower():
        return "no_conversation"
    return "completed"


class CampaignWorker:
    def __init__(self, db: Database, bolna, settings):
        self.db = db
        self.bolna = bolna
        self.settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick_lock = threading.Lock()

    # --- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="campaign-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("worker tick failed")
            self._stop.wait(self.settings.worker_interval_seconds)

    def tick(self, now: datetime | None = None) -> None:
        with self._tick_lock:
            now = now or utcnow()
            for campaign in self.db.campaigns_with_status("running") + self.db.campaigns_with_status("stopped"):
                self.process_campaign(campaign, now)
            self.cleanup(now)

    # --- calls -----------------------------------------------------------------

    def process_campaign(self, campaign: dict, now: datetime) -> None:
        in_flight = self.db.leads_with_status(campaign["id"], IN_FLIGHT)
        for lead in in_flight:
            self.refresh_lead(lead, now)

        in_flight = self.db.leads_with_status(campaign["id"], IN_FLIGHT)
        if campaign["status"] == "running":
            free = self.settings.max_concurrent_calls - len(in_flight)
            if free > 0:
                for lead in self.db.leads_with_status(campaign["id"], ("pending",), limit=free):
                    self.dial(campaign, lead, now)
            if self.db.leads_with_status(campaign["id"], ("pending",), limit=1):
                return
        if not self.db.leads_with_status(campaign["id"], IN_FLIGHT, limit=1):
            if campaign["status"] == "running":
                self.db.update_campaign(campaign["id"], status="completed", finished_at=iso(now))
            elif not campaign.get("finished_at"):
                self.db.update_campaign(campaign["id"], finished_at=iso(now))

    def dial(self, campaign: dict, lead: dict, now: datetime) -> None:
        self.db.update_lead(lead["id"], status="dialing", called_at=iso(now))
        try:
            execution_id = self.bolna.start_call(
                campaign["bolna_agent_id"],
                lead["phone"],
                {"customer_name": lead["name"], "business_name": campaign["business_name"]},
            )
        except BolnaError as exc:
            log.warning("call to lead %s failed: %s", lead["id"], exc)
            self.db.update_lead(lead["id"], status="failed", outcome="failed", error=str(exc)[:500])
            return
        self.db.update_lead(lead["id"], status="queued", execution_id=execution_id)

    def refresh_lead(self, lead: dict, now: datetime) -> None:
        if lead.get("execution_id"):
            try:
                self.apply_execution(lead, self.bolna.get_execution(lead["execution_id"]))
            except BolnaError as exc:
                log.warning("could not fetch execution %s: %s", lead["execution_id"], exc)
            lead = self.db.get_lead_by_execution(lead["execution_id"]) or lead
        if lead["status"] in IN_FLIGHT and lead.get("called_at"):
            started = datetime.fromisoformat(lead["called_at"])
            if now - started > timedelta(minutes=self.settings.call_timeout_minutes):
                self.db.update_lead(lead["id"], status="timed_out", outcome="unknown",
                                    error="No final call status received from Bolna")

    def apply_execution(self, lead: dict, execution: dict) -> None:
        fields = {k: v for k, v in parse_execution(execution).items() if v is not None}
        self.db.update_lead(lead["id"], **fields)

    def handle_webhook(self, payload: dict) -> bool:
        """Bolna posts execution updates here. Only the id is trusted; data is re-fetched from Bolna."""
        execution_id = payload.get("id") or payload.get("execution_id")
        if not execution_id:
            return False
        lead = self.db.get_lead_by_execution(str(execution_id))
        if not lead:
            return False
        try:
            self.apply_execution(lead, self.bolna.get_execution(lead["execution_id"]))
        except BolnaError as exc:
            log.warning("webhook refresh failed for %s: %s", execution_id, exc)
            return False
        return True

    # --- automatic deletion ----------------------------------------------------------

    def cleanup(self, now: datetime) -> int:
        deleted = 0
        for campaign in self.db.expired_campaigns(now):
            if campaign.get("bolna_agent_id"):
                try:
                    self.bolna.delete_agent(campaign["bolna_agent_id"])
                except BolnaError as exc:
                    log.warning("could not delete Bolna agent %s: %s", campaign["bolna_agent_id"], exc)
            self.db.delete_campaign(campaign["id"])
            log.info("deleted expired campaign %s", campaign["id"])
            deleted += 1
        return deleted
