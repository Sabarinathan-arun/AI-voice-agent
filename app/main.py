"""FastAPI app: payment -> sales setup -> lead upload -> AI calls -> results -> export -> auto-deletion."""

import logging
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .bolna import BolnaError, make_bolna_client
from .config import Settings, load_settings
from .db import Database, iso, utcnow
from .export import build_results_workbook
from .leads import LeadFileError, parse_leads
from .payments import PaymentError, make_payment_provider
from .worker import CampaignWorker

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


class SetupIn(BaseModel):
    business_name: str = Field(min_length=1, max_length=200)
    business_description: str = Field(default="", max_length=2000)
    product_details: str = Field(min_length=1, max_length=5000)
    sales_instructions: str = Field(min_length=1, max_length=5000)
    welcome_message: str = Field(default="", max_length=500)
    language: str = Field(default="English", max_length=50)


class VerifyIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=300)


def create_app(settings: Settings | None = None, bolna=None, payments=None, run_worker: bool = True) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.database_path)
    bolna = bolna or make_bolna_client(settings)
    payments = payments or make_payment_provider(settings)
    worker = CampaignWorker(db, bolna, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_worker:
            worker.start()
        yield
        if run_worker:
            worker.stop()

    app = FastAPI(title="AI Sales Voice Agent", lifespan=lifespan)
    app.state.settings, app.state.db, app.state.worker = settings, db, worker

    # --- helpers ---------------------------------------------------------------

    def get_campaign_or_404(campaign_id: str) -> dict:
        campaign = db.get_campaign(campaign_id)
        if not campaign:
            raise HTTPException(404, "Campaign not found. It may have expired and been deleted.")
        return campaign

    def require_status(campaign: dict, *statuses: str) -> None:
        if campaign["status"] not in statuses:
            raise HTTPException(409, f"Not allowed while campaign is '{campaign['status']}'.")

    def mark_paid(campaign_id: str, payment_ref: str) -> None:
        expires = utcnow() + timedelta(hours=settings.retention_hours)
        db.mark_paid(campaign_id, payment_ref, expires)

    def serialize(campaign: dict) -> dict:
        leads = db.get_leads(campaign["id"])
        counts: dict[str, int] = {}
        for lead in leads:
            key = lead["outcome"] or lead["status"]
            counts[key] = counts.get(key, 0) + 1
        done = sum(1 for l in leads if l["outcome"] or l["status"] == "skipped")
        public = {k: v for k, v in campaign.items() if k not in ("payment_ref", "bolna_agent_id")}
        return {
            "campaign": public,
            "stats": {"total": len(leads), "done": done, "by_outcome": counts},
            "leads": [
                {k: l[k] for k in ("id", "row_number", "name", "phone", "status", "outcome", "summary",
                                   "duration_seconds", "recording_url", "transcript", "error", "called_at")}
                for l in leads
            ],
        }

    # --- pages -----------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/campaign", include_in_schema=False)
    def campaign_page():
        return FileResponse(STATIC / "campaign.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/config")
    def public_config():
        return {
            "price_amount": settings.price_amount,
            "price_currency": settings.price_currency,
            "product_name": settings.product_name,
            "max_leads": settings.max_leads_per_campaign,
            "retention_hours": settings.retention_hours,
            "payment_provider": payments.name,
            "bolna_mock": settings.bolna_mock,
        }

    # --- 1. payment --------------------------------------------------------------

    @app.post("/api/campaigns", status_code=201)
    def create_campaign():
        campaign_id = secrets.token_urlsafe(18)
        db.create_campaign(campaign_id, utcnow() + timedelta(hours=settings.unpaid_retention_hours))
        try:
            checkout_url = payments.create_checkout(campaign_id)
        except PaymentError as exc:
            db.delete_campaign(campaign_id)
            raise HTTPException(502, str(exc))
        return {"campaign_id": campaign_id, "checkout_url": checkout_url}

    @app.get("/api/payments/mock/complete", include_in_schema=False)
    def mock_payment_complete(campaign_id: str):
        if payments.name != "mock":
            raise HTTPException(404)
        get_campaign_or_404(campaign_id)
        mark_paid(campaign_id, "mock-payment")
        return RedirectResponse(f"/campaign?id={campaign_id}", status_code=303)

    @app.post("/api/campaigns/{campaign_id}/verify-payment")
    def verify_payment(campaign_id: str, body: VerifyIn):
        campaign = get_campaign_or_404(campaign_id)
        if campaign["status"] == "pending_payment":
            result = payments.verify_session(body.session_id)
            if result and result[0] == campaign_id:
                mark_paid(campaign_id, result[1])
        return {"status": db.get_campaign(campaign_id)["status"]}

    @app.post("/api/payments/stripe/webhook", include_in_schema=False)
    async def stripe_webhook(request: Request):
        if payments.name != "stripe":
            raise HTTPException(404)
        payload = await request.body()
        try:
            event = payments.parse_webhook(payload, request.headers.get("stripe-signature", ""))
        except PaymentError as exc:
            raise HTTPException(400, str(exc))
        if event.get("type") in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            session = event["data"]["object"]
            campaign_id = session.get("client_reference_id")
            if session.get("payment_status") == "paid" and campaign_id and db.get_campaign(campaign_id):
                mark_paid(campaign_id, session["id"])
        return {"received": True}

    # --- campaign state ----------------------------------------------------------

    @app.get("/api/campaigns/{campaign_id}")
    def get_campaign(campaign_id: str):
        return serialize(get_campaign_or_404(campaign_id))

    # --- 2. sales setup ----------------------------------------------------------

    @app.put("/api/campaigns/{campaign_id}/setup")
    def save_setup(campaign_id: str, body: SetupIn):
        campaign = get_campaign_or_404(campaign_id)
        require_status(campaign, "paid")
        db.update_campaign(campaign_id, **body.model_dump())
        return serialize(db.get_campaign(campaign_id))

    # --- 3. lead upload ----------------------------------------------------------

    @app.post("/api/campaigns/{campaign_id}/leads")
    async def upload_leads(campaign_id: str, file: UploadFile = File(...)):
        campaign = get_campaign_or_404(campaign_id)
        require_status(campaign, "paid")
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "File too large (max 5 MB).")
        try:
            result = parse_leads(file.filename or "", content, settings.default_country_code,
                                 settings.max_leads_per_campaign)
        except LeadFileError as exc:
            raise HTTPException(422, str(exc))
        db.replace_leads(campaign_id, result.leads)
        return {"imported": len(result.leads), "rejected": result.errors[:100],
                "rejected_count": len(result.errors)}

    # --- 4. AI calling -----------------------------------------------------------

    @app.post("/api/campaigns/{campaign_id}/start")
    def start_campaign(campaign_id: str, request: Request):
        campaign = get_campaign_or_404(campaign_id)
        require_status(campaign, "paid")
        if not (campaign["business_name"] and campaign["product_details"] and campaign["sales_instructions"]):
            raise HTTPException(422, "Complete the sales setup first.")
        if not db.get_leads(campaign_id):
            raise HTTPException(422, "Upload leads first.")
        webhook_url = f"{settings.app_base_url}/api/webhooks/bolna"
        if not settings.app_base_url.startswith("https://"):
            webhook_url = None  # Bolna cannot reach a local URL; results are polled instead
        try:
            agent_id = bolna.create_agent(campaign, webhook_url)
        except (BolnaError, OSError, ValueError) as exc:
            log.exception("agent creation failed")
            raise HTTPException(502, f"Could not create the voice agent: {exc}")
        db.update_campaign(campaign_id, bolna_agent_id=agent_id, status="running", started_at=iso(utcnow()))
        return serialize(db.get_campaign(campaign_id))

    @app.post("/api/campaigns/{campaign_id}/stop")
    def stop_campaign(campaign_id: str):
        campaign = get_campaign_or_404(campaign_id)
        require_status(campaign, "running")
        db.skip_pending_leads(campaign_id)
        db.update_campaign(campaign_id, status="stopped")
        return serialize(db.get_campaign(campaign_id))

    @app.post("/api/webhooks/bolna", include_in_schema=False)
    async def bolna_webhook(request: Request):
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "Invalid JSON")
        if isinstance(payload, dict):
            worker.handle_webhook(payload)
        return {"received": True}

    # --- 6. export ---------------------------------------------------------------

    @app.get("/api/campaigns/{campaign_id}/export")
    def export_results(campaign_id: str):
        campaign = get_campaign_or_404(campaign_id)
        data = build_results_workbook(campaign, db.get_leads(campaign_id))
        return Response(
            data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="call-results-{campaign_id[:8]}.xlsx"'},
        )

    # --- 7. deletion (automatic via worker, or on demand) ----------------------------

    @app.delete("/api/campaigns/{campaign_id}", status_code=204)
    def delete_campaign(campaign_id: str):
        campaign = get_campaign_or_404(campaign_id)
        if campaign["status"] == "running":
            raise HTTPException(409, "Stop the campaign before deleting it.")
        db.update_campaign(campaign_id, expires_at=iso(utcnow()))
        worker.cleanup(utcnow())
        return Response(status_code=204)

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
