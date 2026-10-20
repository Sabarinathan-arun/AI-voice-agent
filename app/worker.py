"""Background worker: places calls and collects results from Bolna."""

import json
import logging
import re
import threading
from datetime import datetime, timedelta

from .bolna import TERMINAL_STATUSES, BolnaError, call_user_data
from .store import Store, iso, utcnow

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


def _norm(key) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _find_extracted(data, *names):
    """Find a field anywhere in extracted_data. Bolna nests it by category, e.g. {"General": {"Call Summary": ...}}."""
    wanted = {_norm(n) for n in names}
    if isinstance(data, dict):
        for key, value in data.items():
            if _norm(key) in wanted:
                return _plain(value)
        for value in data.values():
            found = _find_extracted(value, *names)
            if found:
                return found
    return None


def _plain(value):
    """Reduce an extracted value such as {"subjective": "..."} or {"value": "..."} to text."""
    if isinstance(value, dict):
        for key in ("value", "subjective", "answer", "text"):
            if value.get(key):
                return str(value[key])
        texts = [str(v) for v in value.values() if isinstance(v, (str, int, float)) and str(v)]
        return "; ".join(texts) or None
    return str(value) if value not in (None, "") else None


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
    summary = _first(execution, "summary") or _find_extracted(extracted, "call summary", "summary")
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
    value = _find_extracted(extracted, "outcome")
    if value:
        return str(value).strip().lower().replace(" ", "_")
    if not transcript or "user:" not in transcript.lower():
        return "no_conversation"
    return "completed"


class CampaignWorker:
    def __init__(self, store: Store, bolna, settings):
        self.store = store
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
            for campaign in self.store.campaigns_with_status("running", "stopped"):
                self.process_campaign(campaign, now)
            self.store.drop_idle(now)

    # --- calls -----------------------------------------------------------------

    def process_campaign(self, campaign: dict, now: datetime) -> None:
        in_flight = self.store.leads_with_status(campaign["id"], IN_FLIGHT)
        for lead in in_flight:
            self.refresh_lead(lead, now)

        in_flight = self.store.leads_with_status(campaign["id"], IN_FLIGHT)
        if campaign["status"] == "running":
            free = self.settings.max_concurrent_calls - len(in_flight)
            if free > 0:
                for lead in self.store.leads_with_status(campaign["id"], ("pending",), limit=free):
                    self.dial(campaign, lead, now)
            if self.store.leads_with_status(campaign["id"], ("pending",), limit=1):
                return
        if not self.store.leads_with_status(campaign["id"], IN_FLIGHT, limit=1) and not campaign.get("finished_at"):
            status = "completed" if campaign["status"] == "running" else campaign["status"]
            self.store.update_campaign(campaign["id"], status=status, finished_at=iso(now))

    def dial(self, campaign: dict, lead: dict, now: datetime) -> None:
        self.store.update_lead(lead["id"], status="dialing", called_at=iso(now))
        try:
            execution_id = self.bolna.start_call(lead["phone"], call_user_data(campaign, lead))
        except BolnaError as exc:
            log.warning("call to lead %s failed: %s", lead["id"], exc)
            self.store.update_lead(lead["id"], status="failed", outcome="failed", error=str(exc)[:500])
            return
        self.store.update_lead(lead["id"], status="queued", execution_id=execution_id)

    def refresh_lead(self, lead: dict, now: datetime) -> None:
        if lead.get("execution_id"):
            try:
                self.apply_execution(lead, self.bolna.get_execution(lead["execution_id"]))
            except BolnaError as exc:
                log.warning("could not fetch execution %s: %s", lead["execution_id"], exc)
            lead = self.store.get_lead_by_execution(lead["execution_id"]) or lead
        if lead["status"] in IN_FLIGHT and lead.get("called_at"):
            started = datetime.fromisoformat(lead["called_at"])
            if now - started > timedelta(minutes=self.settings.call_timeout_minutes):
                self.store.update_lead(lead["id"], status="timed_out", outcome="unknown",
                                    error="No final call status received from Bolna")

    def apply_execution(self, lead: dict, execution: dict) -> None:
        fields = {k: v for k, v in parse_execution(execution).items() if v is not None}
        self.store.update_lead(lead["id"], **fields)

    def handle_webhook(self, payload: dict) -> bool:
        """Bolna posts execution updates here. Only the id is trusted; data is re-fetched from Bolna."""
        execution_id = payload.get("id") or payload.get("execution_id")
        if not execution_id:
            return False
        lead = self.store.get_lead_by_execution(str(execution_id))
        if not lead:
            return False
        try:
            self.apply_execution(lead, self.bolna.get_execution(lead["execution_id"]))
        except BolnaError as exc:
            log.warning("webhook refresh failed for %s: %s", execution_id, exc)
            return False
        return True
