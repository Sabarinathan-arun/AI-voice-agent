"""Payment providers: Stripe Checkout (hosted payment page) and a mock provider for local testing."""

import hashlib
import hmac
import json
import time

import httpx

from .config import Settings

STRIPE_API = "https://api.stripe.com/v1"


class PaymentError(RuntimeError):
    pass


class MockPayments:
    name = "mock"

    def __init__(self, settings: Settings):
        self.settings = settings

    def create_checkout(self, campaign_id: str) -> str:
        return f"{self.settings.app_base_url}/api/payments/mock/complete?campaign_id={campaign_id}"

    def verify_session(self, session_id: str) -> tuple[str, str] | None:
        return None


class StripePayments:
    name = "stripe"

    def __init__(self, settings: Settings):
        if not settings.stripe_secret_key:
            raise RuntimeError("STRIPE_SECRET_KEY is required when PAYMENT_PROVIDER=stripe")
        self.settings = settings
        self._http = httpx.Client(base_url=STRIPE_API, auth=(settings.stripe_secret_key, ""), timeout=30)

    def create_checkout(self, campaign_id: str) -> str:
        base = self.settings.app_base_url
        data = {
            "mode": "payment",
            "client_reference_id": campaign_id,
            "metadata[campaign_id]": campaign_id,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": self.settings.price_currency,
            "line_items[0][price_data][unit_amount]": str(self.settings.price_amount),
            "line_items[0][price_data][product_data][name]": self.settings.product_name,
            "success_url": f"{base}/campaign?id={campaign_id}&session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{base}/?cancelled=1",
        }
        resp = self._http.post("/checkout/sessions", data=data)
        if resp.status_code >= 400:
            raise PaymentError(f"Stripe error: {resp.text[:300]}")
        return resp.json()["url"]

    def verify_session(self, session_id: str) -> tuple[str, str] | None:
        """Return (campaign_id, session_id) if the Checkout Session has been paid."""
        resp = self._http.get(f"/checkout/sessions/{session_id}")
        if resp.status_code >= 400:
            return None
        session = resp.json()
        if session.get("payment_status") != "paid":
            return None
        return session.get("client_reference_id"), session["id"]

    def parse_webhook(self, payload: bytes, signature_header: str, tolerance: int = 300) -> dict:
        """Verify the Stripe-Signature header and return the event."""
        secret = self.settings.stripe_webhook_secret
        if not secret:
            raise PaymentError("STRIPE_WEBHOOK_SECRET is not configured")
        items = [p.strip().split("=", 1) for p in signature_header.split(",") if "=" in p]
        timestamp = next((v for k, v in items if k == "t"), None)
        signatures = [v for k, v in items if k == "v1"]
        if not timestamp or not signatures:
            raise PaymentError("Malformed Stripe-Signature header")
        if abs(time.time() - int(timestamp)) > tolerance:
            raise PaymentError("Stripe webhook timestamp outside tolerance")
        expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, s) for s in signatures):
            raise PaymentError("Invalid Stripe webhook signature")
        return json.loads(payload)


def make_payment_provider(settings: Settings):
    if settings.payment_provider == "stripe":
        return StripePayments(settings)
    if settings.payment_provider == "mock":
        return MockPayments(settings)
    raise RuntimeError(f"Unknown PAYMENT_PROVIDER: {settings.payment_provider}")
