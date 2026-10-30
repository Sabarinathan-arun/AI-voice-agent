"""FastAPI app: sign-in -> wallet top-up -> sales setup -> lead upload -> AI calls -> results -> export.

Everything is saved in the database (Supabase PostgreSQL in production, SQLite locally): accounts, wallet,
payments, usage and campaigns, so restarts never lose a running campaign. Lead data and call results are
temporary and are deleted RESULTS_RETENTION_DAYS (7) after a campaign finishes. Calls are billed per second at
the per-minute rate; unanswered calls are free.
"""

import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator

from .accounts import Accounts
from .bolna import LANGUAGES, BolnaError, default_greeting, load_voices, make_bolna_client
from .config import Settings, load_settings
from .export import build_results_workbook
from .db import Database
from .supabase_auth import AuthError, make_auth
from .leads import LeadFileError, parse_leads
from .payments import PaymentError, make_payment_provider
from .store import Store, iso, utcnow
from .worker import CampaignWorker

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
SESSION_COOKIE = "intuvora_session"
PROFILE_FIELDS = ("business_name", "business_description", "product_details", "sales_instructions",
                  "welcome_message", "language", "voice")


class SessionIn(BaseModel):
    access_token: str = Field(min_length=10, max_length=10000)


class TopupIn(BaseModel):
    amount_rupees: int = Field(gt=0, le=200000)


class SetupIn(BaseModel):
    business_name: str = Field(min_length=1, max_length=200)
    business_description: str = Field(default="", max_length=2000)
    product_details: str = Field(min_length=1, max_length=5000)
    sales_instructions: str = Field(min_length=1, max_length=5000)
    welcome_message: str = Field(default="", max_length=500)
    language: str = Field(default="English", max_length=50)
    voice: str = Field(default="", max_length=100)

    @field_validator("language")
    @classmethod
    def known_language(cls, value: str) -> str:
        if value not in LANGUAGES:
            raise ValueError(f"Choose one of: {', '.join(LANGUAGES)}")
        return value


def create_app(settings: Settings | None = None, bolna=None, payments=None, auth=None,
               run_worker: bool = True) -> FastAPI:
    settings = settings or load_settings()
    database = Database(settings.database_url)
    store = Store(database)
    accounts = Accounts(database)
    bolna = bolna or make_bolna_client(settings)
    payments = payments or make_payment_provider(settings)
    auth = auth or make_auth(settings)
    voices = load_voices(settings.voices_path)
    if not voices:
        raise RuntimeError(f"No voices configured in {settings.voices_path}")
    voices_by_id = {v["id"]: v for v in voices}
    default_voice = voices[0]
    worker = CampaignWorker(store, bolna, settings, accounts)
    secure_cookie = settings.app_base_url.startswith("https://")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_worker:
            worker.start()
        yield
        if run_worker:
            worker.stop()
        database.close()

    app = FastAPI(title="Intuvora AI Sales Agent", lifespan=lifespan)

    @app.middleware("http")
    async def log_timing(request: Request, call_next):
        """Log how long each API request takes (Render -> Logs), to spot slow pages."""
        started = time.perf_counter()
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            ms = (time.perf_counter() - started) * 1000
            (log.warning if ms > 2000 else log.info)("%s %s %d %.0f ms", request.method, request.url.path,
                                                     response.status_code, ms)
        return response
    app.state.settings, app.state.store, app.state.worker, app.state.accounts = settings, store, worker, accounts

    # --- helpers ---------------------------------------------------------------

    def current_user(request: Request) -> dict:
        user = accounts.user_for_session(request.cookies.get(SESSION_COOKIE))
        if not user:
            raise HTTPException(401, "Please log in.")
        return user

    def start_session(response: Response, user: dict) -> None:
        response.set_cookie(SESSION_COOKIE, accounts.create_session(user["id"]), max_age=30 * 24 * 3600,
                            httponly=True, samesite="lax", secure=secure_cookie)

    def get_campaign_or_404(campaign_id: str, user: dict) -> dict:
        campaign = store.get_campaign(campaign_id)
        if not campaign or campaign["user_id"] != user["id"]:
            raise HTTPException(404, f"Campaign not found. Campaign results are kept for "
                                     f"{settings.results_retention_days} days after a campaign finishes.")
        return campaign

    def require_status(campaign: dict, *statuses: str) -> None:
        if campaign["status"] not in statuses:
            raise HTTPException(409, f"Not allowed while campaign is '{campaign['status']}'.")

    def me(user: dict) -> dict:
        return {"id": user["id"], "name": user["name"], "email": user["email"],
                "balance_paise": user["balance_paise"]}

    def serialize(campaign: dict, balance_paise: int | None = None) -> dict:
        leads = store.get_leads(campaign["id"])
        counts: dict[str, int] = {}
        for lead in leads:
            key = lead["outcome"] or lead["status"]
            counts[key] = counts.get(key, 0) + 1
        done = sum(1 for l in leads if l["outcome"] or l["status"] == "skipped")
        booked = sum(1 for l in leads if l.get("callback_time"))
        usage = accounts.campaign_usage(campaign["id"])
        public = {k: v for k, v in campaign.items() if k not in ("bolna_agent_id", "last_activity", "user_id")}
        return {
            "campaign": public,
            "stats": {"total": len(leads), "done": done, "by_outcome": counts, "booked": booked,
                      "answered_calls": usage["calls"],
                      "seconds": usage["seconds"], "cost_paise": usage["amount_paise"]},
            "balance_paise": accounts.balance(campaign["user_id"]) if balance_paise is None else balance_paise,
            "leads": [
                {k: l[k] for k in ("id", "row_number", "name", "phone", "details", "status", "outcome", "summary",
                                   "duration_seconds", "transcript", "called_at", "cost_paise",
                                   "callback_time")}
                for l in leads
            ],
        }

    # --- pages -----------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/login", include_in_schema=False)
    def login_page():
        return FileResponse(STATIC / "login.html")

    @app.get("/reset-password", include_in_schema=False)
    def reset_password_page():
        return FileResponse(STATIC / "reset-password.html")

    @app.get("/dashboard", include_in_schema=False)
    def dashboard_page():
        return FileResponse(STATIC / "dashboard.html")

    @app.get("/campaign", include_in_schema=False)
    def campaign_page():
        return FileResponse(STATIC / "campaign.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/config")
    def public_config():
        return {
            "price_currency": settings.price_currency,
            "price_per_minute_paise": settings.price_per_minute_paise,
            "min_topup_paise": settings.min_topup_paise,
            "low_balance_paise": settings.low_balance_paise,
            "supabase_url": settings.supabase_url,
            "supabase_anon_key": settings.supabase_anon_key,  # public key, meant for the browser
            "results_retention_days": settings.results_retention_days,
            "max_leads": settings.max_leads_per_campaign,
            "languages": list(LANGUAGES),
        }

    @app.get("/api/voices")
    def list_voices():
        """The AI agents customers can choose from: both speak every call language, and each has a default opening
        line per language (used when the customer leaves it empty)."""
        return [{**{k: v.get(k, "") for k in ("id", "name", "gender", "description", "sample_url")},
                 "languages": list(LANGUAGES),
                 "greetings": {language: default_greeting(language, v) for language in LANGUAGES}} for v in voices]

    # --- accounts ----------------------------------------------------------------

    @app.post("/api/auth/session")
    def open_session(body: SessionIn, response: Response):
        """After signing in with Supabase (email/password or Google), exchange its token for our session."""
        try:
            identity = auth.verify(body.access_token)
        except AuthError as exc:
            raise HTTPException(401, str(exc))
        user = accounts.user_for_identity(identity["sub"], identity["email"], identity["name"])
        start_session(response, user)
        return me(user)

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        accounts.delete_session(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    def get_me(user: dict = Depends(current_user)):
        return me(user)

    # --- wallet --------------------------------------------------------------------

    @app.get("/api/wallet")
    def wallet(user: dict = Depends(current_user)):
        return {"balance_paise": user["balance_paise"], "history": accounts.history(user["id"])}

    @app.post("/api/wallet/topups", status_code=201)
    def create_topup(body: TopupIn, user: dict = Depends(current_user)):
        amount = body.amount_rupees * 100
        if amount < settings.min_topup_paise:
            raise HTTPException(422, f"Minimum top-up is ₹{settings.min_topup_paise // 100}.")
        topup_id = accounts.create_topup(user["id"], amount)
        try:
            url, ref = payments.create_checkout(topup_id, amount, "Intuvora wallet top-up",
                                                f"{settings.app_base_url}/dashboard?topup={topup_id}")
        except PaymentError as exc:
            raise HTTPException(502, str(exc))
        accounts.set_topup_payment_ref(topup_id, ref)
        return {"checkout_url": url}

    @app.post("/api/wallet/topups/verify")
    def verify_topups(user: dict = Depends(current_user)):
        """Credit any of this user's top-ups that Razorpay reports as paid (safe to call repeatedly)."""
        credited = 0
        for topup in accounts.pending_topups(user["id"]):
            if payments.is_paid(topup["payment_ref"], topup["id"]) and accounts.credit_topup(topup["id"]):
                credited += topup["amount_paise"]
        balance = accounts.balance(user["id"]) if credited else user["balance_paise"]
        return {"credited_paise": credited, "balance_paise": balance}

    # --- campaigns -----------------------------------------------------------------

    @app.get("/api/campaigns")
    def list_campaigns(user: dict = Depends(current_user)):
        return [{"id": c["id"], "business_name": c["business_name"], "status": c["status"],
                 "created_at": c["created_at"], "leads": c["lead_count"], "done": c["done_count"]}
                for c in store.campaign_summaries(user["id"])]

    @app.post("/api/campaigns", status_code=201)
    def create_campaign(user: dict = Depends(current_user)):
        campaign_id = secrets.token_urlsafe(18)
        profile = accounts.get_profile(user["id"])  # auto-fill the last business details used
        prefill = {k: profile[k] for k in PROFILE_FIELDS if profile.get(k)}
        if prefill:
            prefill["prefilled"] = True
        store.create_campaign(campaign_id, user["id"], prefill)
        return {"campaign_id": campaign_id}

    @app.get("/api/campaigns/{campaign_id}")
    def get_campaign(campaign_id: str, user: dict = Depends(current_user)):
        return serialize(get_campaign_or_404(campaign_id, user), user["balance_paise"])  # polled while calling

    @app.put("/api/campaigns/{campaign_id}/setup")
    def save_setup(campaign_id: str, body: SetupIn, user: dict = Depends(current_user)):
        campaign = get_campaign_or_404(campaign_id, user)
        require_status(campaign, "setup")
        data = body.model_dump()
        if data["voice"] not in voices_by_id:
            data["voice"] = default_voice["id"]
        store.update_campaign(campaign_id, **data)
        accounts.save_profile(user["id"], {k: data[k] for k in PROFILE_FIELDS})
        return serialize(store.get_campaign(campaign_id))

    @app.post("/api/campaigns/{campaign_id}/leads")
    async def upload_leads(campaign_id: str, file: UploadFile = File(...), user: dict = Depends(current_user)):
        campaign = get_campaign_or_404(campaign_id, user)
        require_status(campaign, "setup")
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "File too large (max 5 MB).")
        def save():  # parsing and saving run in a worker thread so other requests aren't held up
            result = parse_leads(file.filename or "", content, settings.default_country_code,
                                 settings.max_leads_per_campaign)
            store.replace_leads(campaign_id, result.leads)
            store.update_campaign(campaign_id, detail_fields=result.detail_fields, lead_file=file.filename)
            return result

        try:
            result = await run_in_threadpool(save)
        except LeadFileError as exc:
            raise HTTPException(422, str(exc))
        return {"imported": len(result.leads), "rejected": result.errors[:100],
                "rejected_count": len(result.errors),
                "detail_columns": [f["label"] for f in result.detail_fields]}

    @app.post("/api/campaigns/{campaign_id}/start")
    def start_campaign(campaign_id: str, user: dict = Depends(current_user)):
        campaign = get_campaign_or_404(campaign_id, user)
        require_status(campaign, "setup")
        if not (campaign["business_name"] and campaign["product_details"] and campaign["sales_instructions"]):
            raise HTTPException(422, "Complete the sales setup first.")
        if not store.get_leads(campaign_id):
            raise HTTPException(422, "Upload leads first.")
        if accounts.balance(user["id"]) < settings.low_balance_paise:
            raise HTTPException(402, f"Your wallet needs at least ₹{settings.low_balance_paise / 100:g} to start "
                                     "calling. Please top up.")
        webhook_url = f"{settings.app_base_url}/api/webhooks/bolna"
        if not settings.app_base_url.startswith("https://"):
            webhook_url = None  # Bolna cannot reach a local URL; results are polled instead
        voice = voices_by_id.get(campaign.get("voice") or "", default_voice)
        if not store.transition_campaign(campaign_id, "setup", "starting"):  # double click / second tab
            raise HTTPException(409, "This campaign is already starting.")
        try:
            agent_id = bolna.create_agent(campaign, webhook_url, voice)
        except (BolnaError, OSError, ValueError) as exc:
            store.transition_campaign(campaign_id, "starting", "setup")
            log.exception("agent creation failed")
            raise HTTPException(502, f"Could not create the voice agent: {exc}")
        store.update_campaign(campaign_id, bolna_agent_id=agent_id, voice=voice["id"], status="running",
                              started_at=iso(utcnow()))
        worker.wake()
        return serialize(store.get_campaign(campaign_id))

    @app.post("/api/campaigns/{campaign_id}/stop")
    def stop_campaign(campaign_id: str, user: dict = Depends(current_user)):
        campaign = get_campaign_or_404(campaign_id, user)
        require_status(campaign, "running")
        store.skip_pending_leads(campaign_id)
        store.update_campaign(campaign_id, status="stopped")
        return serialize(store.get_campaign(campaign_id))

    @app.post("/api/webhooks/bolna", include_in_schema=False)
    async def bolna_webhook(request: Request):
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "Invalid JSON")
        if isinstance(payload, dict):
            await run_in_threadpool(worker.handle_webhook, payload)  # calls Bolna; keep the server responsive
            worker.wake()
        return {"received": True}

    @app.get("/api/campaigns/{campaign_id}/export")
    def export_results(campaign_id: str, user: dict = Depends(current_user)):
        campaign = get_campaign_or_404(campaign_id, user)
        used = voices_by_id.get(campaign.get("voice") or "", default_voice)
        data = build_results_workbook(campaign, store.get_leads(campaign_id), accounts.campaign_usage(campaign_id),
                                      voice_name=used["name"],
                                      price_per_minute_paise=settings.price_per_minute_paise)
        name = re.sub(r"[^A-Za-z0-9]+", "-", campaign.get("business_name") or "campaign").strip("-")[:40] or "campaign"
        return Response(
            data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{name}-call-results.xlsx"'},
        )

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
