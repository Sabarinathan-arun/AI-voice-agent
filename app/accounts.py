"""Customer accounts: sign-in identities, sessions, wallet balance, top-ups, a log of every wallet transaction,
per-campaign usage totals and the last-used business details (for auto-fill).

Money is kept in paise (integer) to avoid rounding errors.
"""

import hashlib
import json
import secrets
import uuid
from datetime import timedelta

from .db import Database, iso, utcnow

SESSION_LIFETIME = timedelta(days=30)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Accounts:
    def __init__(self, db: Database):
        self.db = db

    # --- users and sessions ------------------------------------------------------

    def user_for_identity(self, auth_id: str, email: str, name: str) -> dict:
        """Find the account for a verified sign-in (Supabase Auth user id + verified email), linking an existing
        account with the same email, or create a new one."""
        email = email.strip().lower()
        name = name.strip()[:100] or email.split("@")[0]
        with self.db.tx() as db:
            row = db.execute("SELECT id FROM users WHERE auth_id = ?", (auth_id,)).fetchone()
            if not row:
                row = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
                if row:
                    db.execute("UPDATE users SET auth_id = ? WHERE id = ?", (auth_id, row["id"]))
            if row:
                user_id = row["id"]
                db.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
                # Follow an email change made in Supabase, unless another account already uses that address.
                db.execute("UPDATE users SET email = ? WHERE id = ? AND NOT EXISTS "
                           "(SELECT 1 FROM users other WHERE other.email = ? AND other.id <> ?)",
                           (email, user_id, email, user_id))
            else:
                # DO NOTHING: a second tab signing in at the same moment may have just created it.
                legacy = self.db.legacy_password_column  # older local databases require password_hash
                db.execute("INSERT INTO users (id, email, name, auth_id, created_at" + (", password_hash" if legacy
                           else "") + ") VALUES (?, ?, ?, ?, ?" + (", ''" if legacy else "") + ") ON CONFLICT DO NOTHING",
                           (uuid.uuid4().hex, email, name, auth_id, iso(utcnow())))
                row = (db.execute("SELECT id FROM users WHERE auth_id = ?", (auth_id,)).fetchone()
                       or db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone())
                user_id = row["id"]
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> dict | None:
        with self.db.tx() as db:
            return db.execute("SELECT id, email, name, balance_paise, created_at FROM users WHERE id = ?",
                              (user_id,)).fetchone()

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.db.tx() as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (iso(utcnow()),))
            db.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                       (_token_hash(token), user_id, iso(utcnow() + SESSION_LIFETIME)))
        return token

    def user_for_session(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self.db.tx() as db:  # one query: this runs on every request
            return db.execute("SELECT u.id, u.email, u.name, u.balance_paise, u.created_at FROM sessions s "
                              "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires_at > ?",
                              (_token_hash(token), iso(utcnow()))).fetchone()

    def delete_session(self, token: str | None) -> None:
        if token:
            with self.db.tx() as db:
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    # --- wallet --------------------------------------------------------------------

    def balance(self, user_id: str) -> int:
        user = self.get_user(user_id)
        return user["balance_paise"] if user else 0

    def create_topup(self, user_id: str, amount_paise: int) -> str:
        topup_id = uuid.uuid4().hex
        with self.db.tx() as db:
            db.execute("INSERT INTO topups (id, user_id, amount_paise, created_at) VALUES (?, ?, ?, ?)",
                       (topup_id, user_id, amount_paise, iso(utcnow())))
        return topup_id

    def set_topup_payment_ref(self, topup_id: str, payment_ref: str) -> None:
        with self.db.tx() as db:
            db.execute("UPDATE topups SET payment_ref = ? WHERE id = ?", (payment_ref, topup_id))

    def pending_topups(self, user_id: str, max_age: timedelta = timedelta(days=1)) -> list[dict]:
        """Unpaid top-ups worth re-checking with Razorpay (abandoned checkouts are ignored after a day)."""
        with self.db.tx() as db:
            return db.execute("SELECT * FROM topups WHERE user_id = ? AND status = 'pending' "
                              "AND payment_ref IS NOT NULL AND created_at > ?",
                              (user_id, iso(utcnow() - max_age))).fetchall()

    def _move_money(self, db, user_id: str, amount_paise: int, kind: str, reference: str) -> None:
        """Change the balance and record it in the transaction log, in the caller's transaction."""
        row = db.execute("UPDATE users SET balance_paise = balance_paise + ? WHERE id = ? RETURNING balance_paise",
                         (amount_paise, user_id)).fetchone()
        db.execute("INSERT INTO wallet_transactions (user_id, kind, amount_paise, balance_after_paise, reference, "
                   "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                   (user_id, kind, amount_paise, row["balance_paise"], reference, iso(utcnow())))

    def credit_topup(self, topup_id: str) -> bool:
        """Add a paid top-up to the wallet exactly once."""
        with self.db.tx() as db:
            row = db.execute("UPDATE topups SET status = 'credited', credited_at = ? "
                             "WHERE id = ? AND status = 'pending' RETURNING user_id, amount_paise",
                             (iso(utcnow()), topup_id)).fetchone()
            if not row:
                return False
            self._move_money(db, row["user_id"], row["amount_paise"], "topup", topup_id)
            return True

    def charge_call(self, user_id: str, campaign_id: str, business_name: str | None, started_at: str,
                    seconds: int, amount_paise: int) -> None:
        """Deduct one answered call from the wallet and add it to the campaign's usage line."""
        with self.db.tx() as db:
            self._move_money(db, user_id, -amount_paise, "call", campaign_id)
            db.execute(
                "INSERT INTO usage (campaign_id, user_id, business_name, started_at, calls, seconds, amount_paise) "
                "VALUES (?, ?, ?, ?, 1, ?, ?) ON CONFLICT (campaign_id) DO UPDATE SET calls = usage.calls + 1, "
                "seconds = usage.seconds + excluded.seconds, amount_paise = usage.amount_paise + excluded.amount_paise",
                (campaign_id, user_id, business_name, started_at, seconds, amount_paise),
            )

    def campaign_usage(self, campaign_id: str) -> dict:
        with self.db.tx() as db:
            row = db.execute("SELECT calls, seconds, amount_paise FROM usage WHERE campaign_id = ?",
                             (campaign_id,)).fetchone()
        return row or {"calls": 0, "seconds": 0, "amount_paise": 0}

    def history(self, user_id: str, limit: int = 50) -> list[dict]:
        """Top-ups and campaign charges, newest first."""
        with self.db.tx() as db:
            topups = db.execute("SELECT credited_at AS at, amount_paise FROM topups "
                                "WHERE user_id = ? AND status = 'credited'", (user_id,)).fetchall()
            usage = db.execute("SELECT started_at AS at, business_name, calls, seconds, amount_paise FROM usage "
                               "WHERE user_id = ?", (user_id,)).fetchall()
        items = [{"type": "topup", "at": r["at"], "amount_paise": r["amount_paise"],
                  "description": "Wallet top-up"} for r in topups]
        items += [{"type": "campaign", "at": r["at"], "amount_paise": -r["amount_paise"], "calls": r["calls"],
                   "seconds": r["seconds"], "description": f"Campaign: {r['business_name'] or 'Untitled'}"}
                  for r in usage]
        return sorted(items, key=lambda i: i["at"] or "", reverse=True)[:limit]

    def transactions(self, user_id: str, limit: int = 200) -> list[dict]:
        """The raw wallet log (every credit and debit with the balance after it), newest first."""
        with self.db.tx() as db:
            return db.execute("SELECT kind, amount_paise, balance_after_paise, reference, created_at "
                              "FROM wallet_transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                              (user_id, limit)).fetchall()

    # --- saved business details (auto-fill) ------------------------------------------

    def get_profile(self, user_id: str) -> dict:
        with self.db.tx() as db:
            row = db.execute("SELECT data FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
        return json.loads(row["data"]) if row else {}

    def save_profile(self, user_id: str, data: dict) -> None:
        with self.db.tx() as db:
            db.execute("INSERT INTO profiles (user_id, data) VALUES (?, ?) "
                       "ON CONFLICT (user_id) DO UPDATE SET data = excluded.data", (user_id, json.dumps(data)))
