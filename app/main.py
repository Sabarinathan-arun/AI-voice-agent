"""FastAPI app: payment -> sales setup -> lead upload -> AI calls -> results -> export.

One-time use: each visit is a fresh campaign held in memory only. Nothing is written to disk.
"""

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .bolna import make_bolna_client
from .config import Settings, load_settings
from .export import build_results_workbook
from .leads import LeadFileError, parse_leads
from .payments import PaymentError, make_payment_provider
from .store import Store, iso, utcnow
from .worker import CampaignWorker

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


class SetupIn(BaseModel):
    business_name: str = Field(min_length=1, max_length=200)
    business_description: str = Field(default="", max_length=2000)
    product_details: str = Field(min_length=1, max_length=5000)
    sales_instructions: str = Field(min_length=1, max_length=5000)
    welcome_message: str = Field(default="", max_length=500)
    language: str = Field(default="English", max_length=50)


def create_app(settings: Settings | None = None, bolna=None, payments=None, run_worker: bool = True) -> FastAPI:
    settings = settings or load_settings()
    store = Store()
    bolna = bolna or make_bolna_client(settings)
    payments = payments or make_payment_provider(settings)
    worker = CampaignWorker(store, bolna, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_worker:
            worker.start()
        yield
        if run_worker:
            worker.stop()

    app = FastAPI(title="AI Sales Voice Agent", lifespan=lifespan)
    app.state.settings, app.state.store, app.state.worker = settings, store, worker

    # --- helpers ---------------------------------------------------------------

    def get_campaign_or_404(campaign_id: str) -> dict:
        campaign = store.get_campaign(campaign_id)
        if not campaign:
            raise HTTPException(404, "Campaign not found. Sessions are not saved; please start again.")
        return campaign

    def require_status(campaign: dict, *statuses: str) -> None:
        if campaign["status"] not in statuses:
            raise HTTPException(409, f"Not allowed while campaign is '{campaign['status']}'.")

    def serialize(campaign: dict) -> dict:
        leads = store.get_leads(campaign["id"])
        counts: dict[str, int] = {}
        for lead in leads:
            key = lead["outcome"] or lead["status"]
            counts[key] = counts.get(key, 0) + 1
        done = sum(1 for l in leads if l["outcome"] or l["status"] == "skipped")
        public = {k: v for k, v in campaign.items() if k not in ("payment_ref", "last_activity")}
        return {
            "campaign": public,
            "stats": {"total": len(leads), "done": done, "by_outcome": counts},
            "leads": [
                {k: l[k] for k in ("id", "row_number", "name", "phone", "details", "status", "outcome", "summary",
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
            "max_leads": settings.max_leads_per_campaign,
        }

    # --- 1. payment --------------------------------------------------------------

    @app.post("/api/campaigns", status_code=201)
    def create_campaign():
        campaign_id = secrets.token_urlsafe(18)
        store.create_campaign(campaign_id)
        try:
            checkout_url, payment_ref = payments.create_checkout(campaign_id)
        except PaymentError as exc:
            raise HTTPException(502, str(exc))
        store.update_campaign(campaign_id, payment_ref=payment_ref)
        return {"campaign_id": campaign_id, "checkout_url": checkout_url}

    @app.post("/api/campaigns/{campaign_id}/verify-payment")
    def verify_payment(campaign_id: str):
        campaign = get_campaign_or_404(campaign_id)
        if campaign["status"] == "pending_payment" and campaign["payment_ref"]:
            if payments.is_paid(campaign["payment_ref"], campaign_id):
                store.mark_paid(campaign_id)
        return {"status": store.get_campaign(campaign_id)["status"]}

    # --- campaign state ----------------------------------------------------------

    @app.get("/api/campaigns/{campaign_id}")
    def get_campaign(campaign_id: str):
        return serialize(get_campaign_or_404(campaign_id))

    # --- 2. sales setup ----------------------------------------------------------

    @app.put("/api/campaigns/{campaign_id}/setup")
    def save_setup(campaign_id: str, body: SetupIn):
        campaign = get_campaign_or_404(campaign_id)
        require_status(campaign, "paid")
        store.update_campaign(campaign_id, **body.model_dump())
        return serialize(store.get_campaign(campaign_id))

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
        store.replace_leads(campaign_id, result.leads)
        store.update_campaign(campaign_id, detail_fields=result.detail_fields)
        return {"imported": len(result.leads), "rejected": result.errors[:100],
                "rejected_count": len(result.errors),
                "detail_columns": [f["label"] for f in result.detail_fields]}

    # --- 4. AI calling -----------------------------------------------------------

    @app.post("/api/campaigns/{campaign_id}/start")
    def start_campaign(campaign_id: str):
        campaign = get_campaign_or_404(campaign_id)
        require_status(campaign, "paid")
        if not (campaign["business_name"] and campaign["product_details"] and campaign["sales_instructions"]):
            raise HTTPException(422, "Complete the sales setup first.")
        if not store.get_leads(campaign_id):
            raise HTTPException(422, "Upload leads first.")
        store.update_campaign(campaign_id, status="running", started_at=iso(utcnow()))
        return serialize(store.get_campaign(campaign_id))

    @app.post("/api/campaigns/{campaign_id}/stop")
    def stop_campaign(campaign_id: str):
        campaign = get_campaign_or_404(campaign_id)
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
            worker.handle_webhook(payload)
        return {"received": True}

    # --- 5. export ---------------------------------------------------------------

    @app.get("/api/campaigns/{campaign_id}/export")
    def export_results(campaign_id: str):
        campaign = get_campaign_or_404(campaign_id)
        data = build_results_workbook(campaign, store.get_leads(campaign_id))
        return Response(
            data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="call-results-{campaign_id[:8]}.xlsx"'},
        )

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
