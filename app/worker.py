"""Background worker: places calls, collects results from Bolna and charges answered calls to the wallet."""

import json
import logging
import math
import re
import threading
from datetime import datetime, timedelta

from .bolna import TERMINAL_STATUSES, BolnaError, call_user_data
from .store import IN_FLIGHT, Store, iso, utcnow

log = logging.getLogger(__name__)

IDLE_CHECK_SECONDS = 60
PURGE_EVERY = timedelta(hours=1)
# Bolna fills Outcome / Call booked for / Key points in a follow-up task after the call, so an answered call's
# execution is re-read for this long until its extracted_data appears.
EXTRACTION_WAIT = timedelta(minutes=10)

NOT_REACHED = {"no-answer": "no_answer", "busy": "busy", "failed": "failed", "error": "failed",
               "canceled": "cancelled", "cancelled": "cancelled", "stopped": "cancelled",
               "balance-low": "failed"}
POSITIVE = {"interested", "callback_requested", "callback", "call_back", "yes", "hot", "warm", "booked",
            "meeting_booked", "visit_booked", "demo_booked", "positive"}
NEGATIVE = {"not_interested", "no", "cold", "declined", "negative", "do_not_call", "dnd"}
NEGATIVE_CUES = ("not interested", "no interest", "declined", "refused", "don't call", "do not call",
                 "not looking", "already has", "already have", "said no", "not keen", "busy, no")
POSITIVE_CUES = ("interested", "agreed", "book", "visit", "demo", "meeting", "call back", "callback",
                 "send details", "send the details", "wants to know", "asked about", "follow up", "follow-up")


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
    """Reduce an extracted value such as {"objective": "interested", "subjective": "..."} or {"value": "..."} to
    text. A Bolna extraction's objective answer (one of its options) wins over its free-text one."""
    if isinstance(value, dict):
        for key in ("value", "objective", "subjective", "answer", "text"):
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
    callback = _find_extracted(extracted, "callback time", "callback_time", "call back time", "appointment",
                               "meeting time", "visit time")
    if callback and callback.strip().lower() in ("", "none", "null", "n/a", "na", "no", "empty", "-"):
        callback = None
    fields = {
        "status": status,
        "transcript": transcript,
        "summary": summary,
        "callback_time": callback,
        "key_points": _find_extracted(extracted, "key points", "key_points"),
        "duration_seconds": duration,
        "recording_url": _first(telephony, "recording_url"),
        "extracted_data": json.dumps(extracted) if extracted else None,
    }
    if status in TERMINAL_STATUSES:
        fields["outcome"] = _outcome(status, extracted, transcript, summary, callback)
        if not summary and transcript:
            fields["summary"] = transcript[:300]
        hangup = _first(telephony, "hangup_reason")
        if status not in ("completed",) and hangup:
            fields["error"] = str(hangup)
    return fields


def _outcome(status: str, extracted: dict, transcript: str | None, summary: str | None = None,
             callback: str | None = None) -> str:
    """Answered calls are always classed as interested or not_interested (or wrong_number), never "completed"."""
    if status in NOT_REACHED:
        return NOT_REACHED[status]
    if not transcript or "user:" not in transcript.lower():
        return "no_answer"  # picked up by voicemail or hung up without speaking
    value = (_find_extracted(extracted, "outcome") or "").strip().lower().replace(" ", "_").replace("-", "_")
    if value == "wrong_number":
        return value
    if value in POSITIVE or callback:
        return "interested"
    if value in NEGATIVE:
        return "not_interested"
    # No usable extraction from Bolna: judge from the call summary (negative cues first: "not interested").
    text = (summary or "").lower()
    if any(cue in text for cue in NEGATIVE_CUES):
        return "not_interested"
    if any(cue in text for cue in POSITIVE_CUES):
        return "interested"
    return "not_interested"


def log_call_result(lead: dict, execution: dict, fields: dict) -> None:
    """One line per finished call, to diagnose calls from the server logs: how many times the agent and the
    customer spoke (customer turns 0 = the agent heard nothing), how long it ran and why it ended.
    No transcript text or phone numbers are logged."""
    transcript = fields.get("transcript") or ""
    roles = re.findall(r"^\s*(\w+)\s*:", transcript, flags=re.M)
    telephony = execution.get("telephony_data") or {}
    log.info("call finished: execution=%s status=%s outcome=%s duration=%ss agent_turns=%d customer_turns=%d "
             "hangup=%s error=%s extracted=%s execution_fields=%s", execution.get("id") or lead.get("execution_id"),
             fields.get("status"), fields.get("outcome"), fields.get("duration_seconds"),
             sum(1 for r in roles if r.lower() in ("assistant", "agent", "bot")),
             sum(1 for r in roles if r.lower() in ("user", "customer", "human")),
             telephony.get("hangup_reason") or telephony.get("hangup_by") or "-",
             (execution.get("error_message") or execution.get("error") or "-"),
             _extracted_keys(execution), ",".join(sorted(execution)))


def _extracted_keys(execution: dict) -> str:
    """The field names (not values) of Bolna's extracted_data, or "none"."""
    data = execution.get("extracted_data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return "text"
    return ",".join(sorted(data)) if isinstance(data, dict) and data else "none"


def call_cost_paise(seconds: float, price_per_minute_paise: int) -> int:
    """Per-second billing at the per-minute rate, rounded up to the next paisa."""
    return math.ceil(seconds * price_per_minute_paise / 60) if seconds > 0 else 0


class CampaignWorker:
    def __init__(self, store: Store, bolna, settings, accounts):
        self.store = store
        self.bolna = bolna
        self.settings = settings
        self.accounts = accounts
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick_lock = threading.Lock()
        self._last_purge: datetime | None = None
        self._awaiting_extraction: dict[int, tuple[str, datetime]] = {}  # lead id -> (execution id, give-up time)

    # --- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="campaign-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def wake(self) -> None:
        """Check campaigns now (a campaign just started, or Bolna reported a call)."""
        self._wake.set()

    def _run(self) -> None:
        # Campaigns are saved in the database, so after a restart this simply carries on with them.
        while not self._stop.is_set():
            active = False
            try:
                active = self.tick()
            except Exception:
                log.exception("worker tick failed")
            # Busy: check every few seconds. Idle: rest (so the database can too) until woken or a minute passes.
            self._wake.wait(self.settings.worker_interval_seconds if active else IDLE_CHECK_SECONDS)
            self._wake.clear()

    def tick(self, now: datetime | None = None) -> bool:
        """One pass over the active campaigns, plus the hourly clean-up. Returns True if any campaign is active."""
        with self._tick_lock:
            now = now or utcnow()
            campaigns = self.store.active_campaigns()
            for campaign in campaigns:
                self.process_campaign(campaign, now)
            self.collect_extractions(now)
            if self._last_purge is None or now - self._last_purge >= PURGE_EVERY:
                deleted = self.store.purge_expired(now, timedelta(days=self.settings.results_retention_days))
                if deleted:
                    log.info("deleted %d expired campaign(s) and their lead data", deleted)
                self._last_purge = now
            return bool(campaigns) or bool(self._awaiting_extraction)

    # --- calls -----------------------------------------------------------------

    def process_campaign(self, campaign: dict, now: datetime) -> None:
        in_flight = self.store.leads_with_status(campaign["id"], IN_FLIGHT)
        for lead in in_flight:
            self.refresh_lead(lead, now)

        in_flight = self.store.leads_with_status(campaign["id"], IN_FLIGHT)
        if campaign["status"] == "running":
            # Don't call the next lead while the wallet is below the low-balance limit; resume after a top-up.
            low = self.accounts.balance(campaign["user_id"]) < self.settings.low_balance_paise
            if low != campaign.get("low_balance"):
                self.store.update_campaign(campaign["id"], low_balance=low)
            free = 0 if low else self.settings.max_concurrent_calls - len(in_flight)
            if free > 0:
                for lead in self.store.leads_with_status(campaign["id"], ("pending",), limit=free):
                    self.dial(campaign, lead, now)
            if self.store.leads_with_status(campaign["id"], ("pending",), limit=1):
                return
        if not self.store.leads_with_status(campaign["id"], IN_FLIGHT, limit=1) and not campaign.get("finished_at"):
            if self.store.finish_campaign(campaign["id"], iso(now)):  # keeps "stopped" if stopped meanwhile
                self.remove_agent(campaign)

    def remove_agent(self, campaign: dict) -> None:
        """The campaign's agent is only needed while calling; delete it so nothing is kept at Bolna either."""
        if not campaign.get("bolna_agent_id"):
            return
        try:
            self.bolna.delete_agent(campaign["bolna_agent_id"])
            self.store.update_campaign(campaign["id"], bolna_agent_id=None)
        except BolnaError as exc:
            log.warning("could not delete Bolna agent %s: %s", campaign["bolna_agent_id"], exc)

    def dial(self, campaign: dict, lead: dict, now: datetime) -> None:
        if not self.store.claim_dial(lead["id"]):  # the campaign was stopped since the lead was picked
            return
        self.store.update_lead(lead["id"], called_at=iso(now))
        try:
            execution_id = self.bolna.start_call(
                campaign["bolna_agent_id"], lead["phone"], call_user_data(campaign, lead)
            )
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
        if fields.get("status") in TERMINAL_STATUSES and lead.get("status") not in TERMINAL_STATUSES:
            log_call_result(lead, execution, fields)
        if fields.get("status") == "completed":  # answered: wait for Bolna's follow-up extraction
            extracted = _extracted_keys(execution)
            if extracted == "none" and lead["id"] not in self._awaiting_extraction:
                self._awaiting_extraction[lead["id"]] = (execution.get("id") or lead["execution_id"],
                                                         utcnow() + EXTRACTION_WAIT)
            elif extracted != "none" and self._awaiting_extraction.pop(lead["id"], None):
                log.info("extraction received: execution=%s extracted=%s", execution.get("id"), extracted)
        if fields.get("status") in TERMINAL_STATUSES:
            self.bill(lead, fields.get("duration_seconds") or 0)

    def collect_extractions(self, now: datetime) -> None:
        """Re-read answered calls until Bolna's extraction (Call booked for, Key points) arrives, or give up."""
        for lead_id, (execution_id, give_up) in list(self._awaiting_extraction.items()):
            lead = self.store.get_lead_by_execution(execution_id)
            if lead is None:  # campaign deleted
                self._awaiting_extraction.pop(lead_id, None)
                continue
            try:
                execution = self.bolna.get_execution(execution_id)
            except BolnaError as exc:
                log.warning("could not fetch execution %s: %s", execution_id, exc)
                continue
            self.apply_execution(lead, execution)
            if lead_id in self._awaiting_extraction and now >= give_up:
                self._awaiting_extraction.pop(lead_id, None)
                log.warning("no extraction from Bolna for execution=%s after %d min; execution_fields=%s",
                            execution_id, EXTRACTION_WAIT.seconds // 60, ",".join(sorted(execution)))

    def bill(self, lead: dict, seconds: float) -> None:
        """Charge an answered call once. Unanswered calls (0 seconds) are free."""
        cost = call_cost_paise(seconds, self.settings.price_per_minute_paise)
        if cost <= 0 or not self.store.claim_billing(lead["id"]):
            return
        campaign = self.store.get_campaign(lead["campaign_id"])
        if not campaign:
            return
        self.store.update_lead(lead["id"], cost_paise=cost)
        self.accounts.charge_call(campaign["user_id"], campaign["id"], campaign["business_name"],
                                  campaign["started_at"] or campaign["created_at"], math.ceil(seconds), cost)

    def handle_webhook(self, payload: dict) -> bool:
        """Bolna posts execution updates here. Only the id is trusted; data is re-fetched from Bolna."""
        execution_id = payload.get("id") or payload.get("execution_id")
        if not execution_id:
            return False
        execution_id = str(execution_id)
        try:
            lead = self.store.get_lead_by_execution(execution_id)
            execution = self.bolna.get_execution(execution_id)
        except BolnaError as exc:
            log.warning("webhook refresh failed for %s: %s", execution_id, exc)
            return False
        if not lead:
            # Not one we know: maybe the server stopped between placing the call and saving its id.
            # Match it (as fetched from Bolna, not as posted) by the campaign's agent and the number called.
            telephony = execution.get("telephony_data") or {}
            phone = execution.get("user_number") or telephony.get("to_number") or ""
            lead = self.store.find_lost_lead(str(execution.get("agent_id") or ""), str(phone))
            if not lead:
                return False
            self.store.update_lead(lead["id"], execution_id=execution_id, error=None)
            lead = {**lead, "execution_id": execution_id}
        self.apply_execution(lead, execution)
        return True
