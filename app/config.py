"""Application settings, read from environment variables (and an optional .env file)."""

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    app_base_url: str
    database_path: str

    payment_provider: str
    stripe_secret_key: str
    stripe_webhook_secret: str
    price_amount: int
    price_currency: str
    product_name: str

    bolna_mock: bool
    bolna_api_key: str
    bolna_api_base: str
    bolna_from_number: str
    bolna_agent_template: str

    max_leads_per_campaign: int
    max_concurrent_calls: int
    default_country_code: str
    call_timeout_minutes: int
    worker_interval_seconds: int

    retention_hours: int
    unpaid_retention_hours: int


def load_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    template = os.environ.get("BOLNA_AGENT_TEMPLATE", "app/bolna_agent_template.json")
    if not os.path.isabs(template):
        template = str(ROOT / template)
    db_path = os.environ.get("DATABASE_PATH", "data/app.db")
    if db_path != ":memory:" and not os.path.isabs(db_path):
        db_path = str(ROOT / db_path)
    return Settings(
        app_base_url=os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/"),
        database_path=db_path,
        payment_provider=os.environ.get("PAYMENT_PROVIDER", "mock").lower(),
        stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY", ""),
        stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET", ""),
        price_amount=_int("PRICE_AMOUNT", 99900),
        price_currency=os.environ.get("PRICE_CURRENCY", "inr").lower(),
        product_name=os.environ.get("PRODUCT_NAME", "AI Sales Calling Campaign"),
        bolna_mock=_bool("BOLNA_MOCK", True),
        bolna_api_key=os.environ.get("BOLNA_API_KEY", ""),
        bolna_api_base=os.environ.get("BOLNA_API_BASE", "https://api.bolna.ai").rstrip("/"),
        bolna_from_number=os.environ.get("BOLNA_FROM_NUMBER", ""),
        bolna_agent_template=template,
        max_leads_per_campaign=_int("MAX_LEADS_PER_CAMPAIGN", 500),
        max_concurrent_calls=_int("MAX_CONCURRENT_CALLS", 3),
        default_country_code=os.environ.get("DEFAULT_COUNTRY_CODE", "91").lstrip("+"),
        call_timeout_minutes=_int("CALL_TIMEOUT_MINUTES", 30),
        worker_interval_seconds=_int("WORKER_INTERVAL_SECONDS", 5),
        retention_hours=_int("RETENTION_HOURS", 72),
        unpaid_retention_hours=_int("UNPAID_RETENTION_HOURS", 24),
    )
