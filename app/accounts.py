"""Customer accounts, stored in SQLite: logins, wallet balance, top-ups, usage history and the last-used
business details (for auto-fill).

Lead lists, call transcripts and results are NOT stored here; they stay in memory for 24 hours (store.py).
Money is kept in paise (integer) to avoid rounding errors.
"""

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from .store import iso, utcnow

SESSION_LIFETIME = timedelta(days=30)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    balance_paise INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topups (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount_paise  INTEGER NOT NULL,
    payment_ref   TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending | credited
    created_at    TEXT NOT NULL,
    credited_at   TEXT
);
CREATE TABLE IF NOT EXISTS usage (
    campaign_id   TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    business_name TEXT,
    started_at    TEXT NOT NULL,
    calls         INTEGER NOT NULL DEFAULT 0,      -- answered (billed) calls
    seconds       INTEGER NOT NULL DEFAULT 0,
    amount_paise  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS profiles (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_topups_user ON topups(user_id);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage(user_id);
"""


class AccountError(ValueError):
    pass


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _check_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, _ = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(_hash_password(password, bytes.fromhex(salt_hex)), stored)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Accounts:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # --- users and sessions ------------------------------------------------------

    def create_user(self, email: str, name: str, password: str) -> dict:
        email = email.strip().lower()
        if not EMAIL_RE.match(email):
            raise AccountError("Enter a valid email address.")
        if len(password) < 8:
            raise AccountError("Password must be at least 8 characters.")
        user = {"id": uuid.uuid4().hex, "email": email, "name": name.strip()[:100] or email.split("@")[0]}
        try:
            with self._db() as db:
                db.execute(
                    "INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                    (user["id"], email, user["name"], _hash_password(password), iso(utcnow())),
                )
        except sqlite3.IntegrityError:
            raise AccountError("An account with this email already exists. Please log in.")
        return self.get_user(user["id"])

    def authenticate(self, email: str, password: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
        if not row or not _check_password(password, row["password_hash"]):
            return None
        return self.get_user(row["id"])

    def get_user(self, user_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT id, email, name, balance_paise, created_at FROM users WHERE id = ?",
                             (user_id,)).fetchone()
        return dict(row) if row else None

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._db() as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (iso(utcnow()),))
            db.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                       (_token_hash(token), user_id, iso(utcnow() + SESSION_LIFETIME)))
        return token

    def user_for_session(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self._db() as db:
            row = db.execute("SELECT user_id FROM sessions WHERE token_hash = ? AND expires_at > ?",
                             (_token_hash(token), iso(utcnow()))).fetchone()
        return self.get_user(row["user_id"]) if row else None

    def delete_session(self, token: str | None) -> None:
        if token:
            with self._db() as db:
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    # --- wallet --------------------------------------------------------------------

    def balance(self, user_id: str) -> int:
        user = self.get_user(user_id)
        return user["balance_paise"] if user else 0

    def create_topup(self, user_id: str, amount_paise: int) -> str:
        topup_id = uuid.uuid4().hex
        with self._db() as db:
            db.execute("INSERT INTO topups (id, user_id, amount_paise, created_at) VALUES (?, ?, ?, ?)",
                       (topup_id, user_id, amount_paise, iso(utcnow())))
        return topup_id

    def set_topup_payment_ref(self, topup_id: str, payment_ref: str) -> None:
        with self._db() as db:
            db.execute("UPDATE topups SET payment_ref = ? WHERE id = ?", (payment_ref, topup_id))

    def pending_topups(self, user_id: str, max_age: timedelta = timedelta(days=7)) -> list[dict]:
        """Unpaid top-ups worth re-checking with Razorpay (abandoned checkouts are ignored after a week)."""
        with self._db() as db:
            rows = db.execute("SELECT * FROM topups WHERE user_id = ? AND status = 'pending' AND payment_ref IS NOT NULL "
                              "AND created_at > ?", (user_id, iso(utcnow() - max_age))).fetchall()
        return [dict(r) for r in rows]

    def credit_topup(self, topup_id: str) -> bool:
        """Add a paid top-up to the wallet exactly once."""
        with self._db() as db:
            row = db.execute("SELECT user_id, amount_paise FROM topups WHERE id = ?", (topup_id,)).fetchone()
            if not row:
                return False
            cur = db.execute("UPDATE topups SET status = 'credited', credited_at = ? WHERE id = ? AND status = 'pending'",
                             (iso(utcnow()), topup_id))
            if cur.rowcount != 1:
                return False
            db.execute("UPDATE users SET balance_paise = balance_paise + ? WHERE id = ?",
                       (row["amount_paise"], row["user_id"]))
            return True

    def charge_call(self, user_id: str, campaign_id: str, business_name: str | None, started_at: str,
                    seconds: int, amount_paise: int) -> None:
        """Deduct one answered call from the wallet and add it to the campaign's usage line."""
        with self._db() as db:
            db.execute("UPDATE users SET balance_paise = balance_paise - ? WHERE id = ?", (amount_paise, user_id))
            db.execute(
                "INSERT INTO usage (campaign_id, user_id, business_name, started_at, calls, seconds, amount_paise) "
                "VALUES (?, ?, ?, ?, 1, ?, ?) ON CONFLICT(campaign_id) DO UPDATE SET "
                "calls = calls + 1, seconds = seconds + excluded.seconds, amount_paise = amount_paise + excluded.amount_paise",
                (campaign_id, user_id, business_name, started_at, seconds, amount_paise),
            )

    def campaign_usage(self, campaign_id: str) -> dict:
        with self._db() as db:
            row = db.execute("SELECT calls, seconds, amount_paise FROM usage WHERE campaign_id = ?",
                             (campaign_id,)).fetchone()
        return dict(row) if row else {"calls": 0, "seconds": 0, "amount_paise": 0}

    def history(self, user_id: str, limit: int = 50) -> list[dict]:
        """Top-ups and campaign charges, newest first."""
        with self._db() as db:
            topups = db.execute(
                "SELECT credited_at AS at, amount_paise FROM topups WHERE user_id = ? AND status = 'credited'",
                (user_id,)).fetchall()
            usage = db.execute(
                "SELECT started_at AS at, business_name, calls, seconds, amount_paise FROM usage WHERE user_id = ?",
                (user_id,)).fetchall()
        items = [{"type": "topup", "at": r["at"], "amount_paise": r["amount_paise"],
                  "description": "Wallet top-up"} for r in topups]
        items += [{"type": "campaign", "at": r["at"], "amount_paise": -r["amount_paise"], "calls": r["calls"],
                   "seconds": r["seconds"], "description": f"Campaign: {r['business_name'] or 'Untitled'}"}
                  for r in usage]
        return sorted(items, key=lambda i: i["at"] or "", reverse=True)[:limit]

    # --- saved business details (auto-fill) ------------------------------------------

    def get_profile(self, user_id: str) -> dict:
        with self._db() as db:
            row = db.execute("SELECT data FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
        return json.loads(row["data"]) if row else {}

    def save_profile(self, user_id: str, data: dict) -> None:
        with self._db() as db:
            db.execute("INSERT INTO profiles (user_id, data) VALUES (?, ?) "
                       "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data", (user_id, json.dumps(data)))
