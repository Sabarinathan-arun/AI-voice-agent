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


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    app_base_url: str
    database_path: str

    razorpay_key_id: str
    razorpay_key_secret: str
    price_currency: str
    price_per_minute_paise: int
    min_topup_paise: int
    low_balance_paise: int

    google_client_id: str

    bolna_api_key: str
    bolna_api_base: str
    bolna_from_number: str
    bolna_agent_template: str
    voices_path: str

    max_leads_per_campaign: int
    max_concurrent_calls: int
    default_country_code: str
    call_timeout_minutes: int
    worker_interval_seconds: int


def load_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    template = os.environ.get("BOLNA_AGENT_TEMPLATE", "app/bolna_agent_template.json")
    if not os.path.isabs(template):
        template = str(ROOT / template)
    voices = os.environ.get("VOICES_FILE", "app/voices.json")
    if not os.path.isabs(voices):
        voices = str(ROOT / voices)
    db_path = os.environ.get("DATABASE_PATH", "data/accounts.db")
    if db_path != ":memory:" and not os.path.isabs(db_path):
        db_path = str(ROOT / db_path)
    return Settings(
        app_base_url=os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/"),
        database_path=db_path,
        razorpay_key_id=os.environ.get("RAZORPAY_KEY_ID", ""),
        razorpay_key_secret=os.environ.get("RAZORPAY_KEY_SECRET", ""),
        price_currency=os.environ.get("PRICE_CURRENCY", "inr").lower(),
        price_per_minute_paise=_int("PRICE_PER_MINUTE_PAISE", 1000),
        min_topup_paise=_int("MIN_TOPUP_PAISE", 10000),
        low_balance_paise=_int("LOW_BALANCE_PAISE", 5000),
        google_client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
        bolna_api_key=os.environ.get("BOLNA_API_KEY", ""),
        bolna_api_base=os.environ.get("BOLNA_API_BASE", "https://api.bolna.ai").rstrip("/"),
        bolna_from_number=os.environ.get("BOLNA_FROM_NUMBER", ""),
        bolna_agent_template=template,
        voices_path=voices,
        max_leads_per_campaign=_int("MAX_LEADS_PER_CAMPAIGN", 500),
        max_concurrent_calls=_int("MAX_CONCURRENT_CALLS", 3),
        default_country_code=os.environ.get("DEFAULT_COUNTRY_CODE", "91").lstrip("+"),
        call_timeout_minutes=_int("CALL_TIMEOUT_MINUTES", 30),
        worker_interval_seconds=_int("WORKER_INTERVAL_SECONDS", 5),
    )
