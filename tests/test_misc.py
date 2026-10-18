import hashlib
import hmac
import io
import json
import time
import dataclasses

import pytest
from openpyxl import load_workbook

from app.bolna import build_agent_payload
from app.export import build_results_workbook
from app.payments import PaymentError, StripePayments


def test_export_neutralises_formulas():
    data = build_results_workbook({"business_name": "X"}, [{"name": "=HYPERLINK(\"evil\")", "phone": "+919876543210", "summary": "+cmd"}])
    ws = load_workbook(io.BytesIO(data)).active
    assert ws["A2"].value.startswith("'=")
    assert ws["B2"].value == "+919876543210"
    assert ws["E2"].value == "'+cmd"


def test_agent_payload(settings):
    campaign = {"id": "abcdefgh123", "business_name": "Acme", "business_description": "",
                "product_details": "Solar", "sales_instructions": "Book a visit", "welcome_message": "",
                "language": "Hindi"}
    payload = build_agent_payload(settings.bolna_agent_template, campaign, "https://x/hook")
    cfg = payload["agent_config"]
    assert cfg["webhook_url"] == "https://x/hook"
    assert "Acme" in cfg["agent_welcome_message"] and "{customer_name}" in cfg["agent_welcome_message"]
    prompt = payload["agent_prompts"]["task_1"]["system_prompt"]
    assert "Book a visit" in prompt and "Hindi" in prompt and "{customer_name}" in prompt


def test_stripe_signature(settings):
    s = StripePayments(dataclasses.replace(settings, stripe_secret_key="sk_test", stripe_webhook_secret="whsec"))
    body = json.dumps({"type": "checkout.session.completed"}).encode()
    t = str(int(time.time()))
    sig = hmac.new(b"whsec", f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    assert s.parse_webhook(body, f"t={t},v1={sig}")["type"] == "checkout.session.completed"
    with pytest.raises(PaymentError):
        s.parse_webhook(body, f"t={t},v1={'0' * 64}")
    with pytest.raises(PaymentError):
        s.parse_webhook(body, f"t={int(t) - 1000},v1={sig}")
