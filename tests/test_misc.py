import dataclasses
import io
import json

import httpx
from openpyxl import load_workbook

from app.bolna import call_user_data
from app.export import build_results_workbook
from app.payments import RazorpayPayments


def test_export_neutralises_formulas():
    data = build_results_workbook({"business_name": "X"},
                                  [{"name": "=HYPERLINK(\"evil\")", "phone": "+919876543210", "summary": "+cmd"}])
    ws = load_workbook(io.BytesIO(data)).active
    assert ws["A2"].value.startswith("'=")
    assert ws["B2"].value == "+919876543210"
    assert ws["E2"].value == "'+cmd"


def test_call_user_data():
    campaign = {"business_name": "Acme", "business_description": "", "product_details": "Solar",
                "sales_instructions": "Book a visit", "welcome_message": "Hi {customer_name}, {business_name} here",
                "language": "Hindi", "detail_fields": [{"key": "city", "label": "City"},
                                                       {"key": "plan", "label": "Plan"}]}
    lead = {"name": "Asha", "details": {"city": "Chennai", "plan": ""}}
    data = call_user_data(campaign, lead)
    assert data["welcome_message"] == "Hi Asha, Acme here"
    assert data["language"] == "Hindi" and data["product_details"] == "Solar"
    assert data["customer_details"] == "City: Chennai"
    assert data["city"] == "Chennai" and data["plan"] == ""
    campaign["welcome_message"] = ""
    assert call_user_data(campaign, {"name": "Ravi"})["welcome_message"].startswith("Hello Ravi, I'm calling from Acme")
    assert call_user_data(campaign, {"name": "Ravi"})["customer_details"] == "none"


def test_razorpay_payment_link(settings):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["amount"] == settings.price_amount and body["currency"] == "INR"
            assert body["reference_id"] == "camp1" and body["callback_method"] == "get"
            assert body["callback_url"].endswith("/campaign?id=camp1")
            return httpx.Response(200, json={"id": "plink_1", "short_url": "https://rzp.io/i/x"})
        status = "paid" if request.url.path.endswith("plink_paid") else "created"
        return httpx.Response(200, json={"id": "x", "status": status, "reference_id": "camp1"})

    rp = RazorpayPayments(dataclasses.replace(settings, razorpay_key_id="rzp_test_x", razorpay_key_secret="s"))
    rp._http = httpx.Client(base_url="https://api.razorpay.com/v1", transport=httpx.MockTransport(handler),
                            auth=("rzp_test_x", "s"))
    assert rp.create_checkout("camp1") == ("https://rzp.io/i/x", "plink_1")
    assert seen[0].headers["authorization"].startswith("Basic ")
    assert rp.is_paid("plink_paid", "camp1") is True
    assert rp.is_paid("plink_paid", "other-campaign") is False
    assert rp.is_paid("plink_open", "camp1") is False
