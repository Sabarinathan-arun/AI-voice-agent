"""Razorpay payments via Payment Links: Razorpay hosts the payment page (UPI, cards, netbanking, wallets).

Flow: create a payment link for the campaign -> customer pays on Razorpay -> Razorpay redirects back
to the campaign page -> the server asks Razorpay whether that link is paid.
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

    def create_checkout(self, campaign_id: str) -> tuple[str, str]:
        """Return (payment_url, payment_link_id)."""
        body = {
            "amount": self.settings.price_amount,
            "currency": self.settings.price_currency.upper(),
            "description": self.settings.product_name,
            "reference_id": campaign_id,
            "callback_url": f"{self.settings.app_base_url}/campaign?id={campaign_id}",
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

    def is_paid(self, payment_link_id: str, campaign_id: str) -> bool:
        """Ask Razorpay directly; never trust the redirect's query string on its own."""
        try:
            resp = self._http.get(f"/payment_links/{payment_link_id}")
        except httpx.HTTPError:
            return False
        if resp.status_code >= 400:
            return False
        link = resp.json()
        return link.get("status") == "paid" and link.get("reference_id") == campaign_id


def make_payment_provider(settings: Settings):
    return RazorpayPayments(settings)
