import dataclasses
import io
import json

import httpx
from openpyxl import load_workbook

from app.bolna import build_agent_payload, call_user_data
from app.export import build_results_workbook
from app.payments import RazorpayPayments


def test_export_neutralises_formulas():
    data = build_results_workbook({"business_name": "X"},
                                  [{"name": "=HYPERLINK(\"evil\")", "phone": "+919876543210", "summary": "+cmd"}])
    ws = load_workbook(io.BytesIO(data)).active
    assert ws["A2"].value.startswith("'=")
    assert ws["B2"].value == "+919876543210"
    assert ws["E2"].value == "'+cmd"


def test_agent_payload_from_customer_details(settings):
    campaign = {"id": "abcdefgh123", "business_name": "Acme", "business_description": "Solar since 2015",
                "product_details": "3kW system", "sales_instructions": "Book a visit",
                "welcome_message": "Hi {customer_name}, {business_name} here", "language": "Hindi"}
    payload = build_agent_payload(settings.bolna_agent_template, campaign, "https://x/hook")
    cfg = payload["agent_config"]
    assert cfg["agent_welcome_message"] == "Hi {{customer_name}}, Acme here"
    assert cfg["webhook_url"] == "https://x/hook" and cfg["call_summary_enabled"] is True
    assert cfg["tasks"][0]["tools_config"]["transcriber"]["language"] == "hi"
    prompt = payload["agent_prompts"]["task_1"]["system_prompt"]
    for text in ("Acme", "Solar since 2015", "3kW system", "Book a visit", "Speak in Hindi",
                 "{{customer_name}}", "{{customer_details}}"):
        assert text in prompt
    campaign["welcome_message"] = ""
    assert build_agent_payload(settings.bolna_agent_template, campaign, None)["agent_config"][
        "agent_welcome_message"] == "Hello {{customer_name}}, I'm calling from Acme. Do you have a minute?"


def test_call_user_data():
    campaign = {"detail_fields": [{"key": "city", "label": "City"}, {"key": "plan", "label": "Plan"}]}
    data = call_user_data(campaign, {"name": "Asha", "details": {"city": "Chennai", "plan": ""}})
    assert data == {"customer_name": "Asha", "customer_details": "City: Chennai", "city": "Chennai", "plan": ""}
    assert call_user_data({}, {"name": "Ravi"}) == {"customer_name": "Ravi", "customer_details": "none"}


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
