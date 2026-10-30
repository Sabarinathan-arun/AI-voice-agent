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
        # .env wins over values already in the environment, so edits take effect even in a terminal that
        # pre-loaded an older copy of the file (VS Code's python.terminal.useEnvFile).
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    app_base_url: str
    database_url: str

    razorpay_key_id: str
    razorpay_key_secret: str
    price_currency: str
    price_per_minute_paise: int
    min_topup_paise: int
    low_balance_paise: int

    supabase_url: str
    supabase_anon_key: str

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
    results_retention_days: int
    language_switching: bool


def load_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    template = os.environ.get("BOLNA_AGENT_TEMPLATE", "app/bolna_agent_template.json")
    if not os.path.isabs(template):
        template = str(ROOT / template)
    voices = os.environ.get("VOICES_FILE", "app/voices.json")
    if not os.path.isabs(voices):
        voices = str(ROOT / voices)
    # Supabase/PostgreSQL connection string; without one, a local SQLite file is used (development and tests).
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        database_url = os.environ.get("DATABASE_PATH", "data/accounts.db")
        if database_url != ":memory:" and not os.path.isabs(database_url):
            database_url = str(ROOT / database_url)
    return Settings(
        app_base_url=os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/"),
        database_url=database_url,
        razorpay_key_id=os.environ.get("RAZORPAY_KEY_ID", ""),
        razorpay_key_secret=os.environ.get("RAZORPAY_KEY_SECRET", ""),
        price_currency=os.environ.get("PRICE_CURRENCY", "inr").lower(),
        price_per_minute_paise=_int("PRICE_PER_MINUTE_PAISE", 1000),
        min_topup_paise=_int("MIN_TOPUP_PAISE", 10000),
        low_balance_paise=_int("LOW_BALANCE_PAISE", 2000),
        supabase_url=os.environ.get("SUPABASE_URL", "").strip().rstrip("/"),
        supabase_anon_key=os.environ.get("SUPABASE_ANON_KEY", "").strip(),
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
        results_retention_days=_int("RESULTS_RETENTION_DAYS", 7),
        # false: each call stays in the campaign language (no multilingual pools at Bolna)
        language_switching=os.environ.get("LANGUAGE_SWITCHING", "true").strip().lower() not in ("0", "false", "no", "off"),
    )
