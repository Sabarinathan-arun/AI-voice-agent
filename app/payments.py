"""Razorpay payments via Payment Links: Razorpay hosts the payment page (UPI, cards, netbanking, wallets).

Used for wallet top-ups: create a payment link -> customer pays on Razorpay -> Razorpay redirects back to the
dashboard -> the server asks Razorpay whether that link is paid, then credits the wallet once.
"""

import httpx

from .config import Settings

RAZORPAY_API = "https://api.razorpay.com/v1"


class PaymentError(RuntimeError):
    pass


class RazorpayPayments:
    def __init__(self, settings: Settings):
        if not (settings.razorpay_key_id and settings.razorpay_key_secret):
            raise RuntimeError("RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are required")
        self.settings = settings
        self._http = httpx.Client(
            base_url=RAZORPAY_API, auth=(settings.razorpay_key_id, settings.razorpay_key_secret), timeout=30
        )

    def create_checkout(self, reference_id: str, amount_paise: int, description: str, callback_url: str) -> tuple[str, str]:
        """Return (payment_url, payment_link_id)."""
        body = {
            "amount": amount_paise,
            "currency": self.settings.price_currency.upper(),
            "description": description,
            "reference_id": reference_id,
            "callback_url": callback_url,
            "callback_method": "get",
        }
        try:
            resp = self._http.post("/payment_links", json=body)
        except httpx.HTTPError as exc:
            raise PaymentError(f"Could not reach Razorpay: {exc}") from exc
        if resp.status_code >= 400:
            raise PaymentError(f"Razorpay error: {resp.text[:300]}")
        link = resp.json()
        return link["short_url"], link["id"]

    def is_paid(self, payment_link_id: str, reference_id: str) -> bool:
        """Ask Razorpay directly; never trust the redirect's query string on its own."""
        try:
            resp = self._http.get(f"/payment_links/{payment_link_id}")
        except httpx.HTTPError:
            return False
        if resp.status_code >= 400:
            return False
        link = resp.json()
        return link.get("status") == "paid" and link.get("reference_id") == reference_id


def make_payment_provider(settings: Settings):
    return RazorpayPayments(settings)
