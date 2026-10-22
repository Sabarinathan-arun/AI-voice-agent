import dataclasses
import io
import json

import httpx
from openpyxl import load_workbook

from app.bolna import build_agent_payload, call_user_data
from app.export import build_results_workbook
from app.payments import RazorpayPayments


def test_export_neutralises_formulas():
    data = build_results_workbook({"business_name": "=cmd()"},
                                  [{"name": "=HYPERLINK(\"evil\")", "phone": "+919876543210", "summary": "+cmd"}])
    wb = load_workbook(io.BytesIO(data))
    ws = wb["Results"]
    assert ws["B2"].value.startswith("'=")
    assert ws["C2"].value == "+919876543210"
    assert ws["G2"].value == "'+cmd"
    summary = {r[0]: r[1] for r in wb["Summary"].iter_rows(values_only=True) if r[0]}
    assert summary["Business name"] == "'=cmd()"


def test_agent_payload_from_customer_details(settings):
    campaign = {"id": "abcdefgh123", "business_name": "Acme", "business_description": "Solar since 2015",
                "product_details": "3kW system", "sales_instructions": "Book a visit",
                "welcome_message": "Hi {customer_name}, {business_name} here", "language": "Hindi"}
    voice = {"id": "v2", "name": "Kajal", "provider": "polly", "provider_config": {"voice": "Kajal", "engine": "neural"}}
    payload = build_agent_payload(settings.bolna_agent_template, campaign, "https://x/hook", voice)
    cfg = payload["agent_config"]
    llm = cfg["tasks"][0]["tools_config"]["llm_agent"]["llm_config"]
    assert "interested or not_interested" in llm["extraction_details"] and "callback_time" in llm["extraction_details"]
    assert llm["summarization_details"] and llm["request_json"] is False
    assert "extraction_details" not in cfg  # Bolna reads it from llm_config
    synth = cfg["tasks"][0]["tools_config"]["synthesizer"]
    assert synth["provider"] == "polly" and synth["provider_config"] == {"voice": "Kajal", "engine": "neural"}
    assert cfg["agent_welcome_message"] == "Hi {{customer_name}}, Acme here"
    assert cfg["webhook_url"] == "https://x/hook" and cfg["call_summary_enabled"] is True
    assert cfg["tasks"][0]["tools_config"]["transcriber"]["language"] == "hi"
    prompt = payload["agent_prompts"]["task_1"]["system_prompt"]
    assert "Your name is Kajal" in prompt and "Sounding natural in Hindi" in prompt
    for text in ("Acme", "Solar since 2015", "3kW system", "Book a visit", "Speak in Hindi",
                 "{{customer_name}}", "{{customer_details}}", "How to sound human", "AI assistant calling for Acme"):
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
            assert body["amount"] == 50000 and body["currency"] == "INR"
            assert body["reference_id"] == "camp1" and body["callback_method"] == "get"
            assert body["callback_url"] == "https://x/dashboard?topup=camp1"
            return httpx.Response(200, json={"id": "plink_1", "short_url": "https://rzp.io/i/x"})
        status = "paid" if request.url.path.endswith("plink_paid") else "created"
        return httpx.Response(200, json={"id": "x", "status": status, "reference_id": "camp1"})

    rp = RazorpayPayments(dataclasses.replace(settings, razorpay_key_id="rzp_test_x", razorpay_key_secret="s"))
    rp._http = httpx.Client(base_url="https://api.razorpay.com/v1", transport=httpx.MockTransport(handler),
                            auth=("rzp_test_x", "s"))
    assert rp.create_checkout("camp1", 50000, "Top-up", "https://x/dashboard?topup=camp1") == ("https://rzp.io/i/x", "plink_1")
    assert seen[0].headers["authorization"].startswith("Basic ")
    assert rp.is_paid("plink_paid", "camp1") is True
    assert rp.is_paid("plink_paid", "other-campaign") is False
    assert rp.is_paid("plink_open", "camp1") is False


def test_voices_file_is_valid(settings):
    from app.bolna import load_voices
    voices = load_voices(settings.voices_path)
    assert [v["name"] for v in voices] == ["Arjun", "Kavya"]
    assert len({v["id"] for v in voices}) == len(voices)
    for v in voices:
        assert v["name"] and v["provider"] and v["provider_config"]


def test_google_token_checks(settings):
    from app.google_auth import GoogleAuth, GoogleAuthError
    import pytest

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params["id_token"]
        base = {"iss": "https://accounts.google.com", "sub": "123", "email": "a@gmail.com",
                "email_verified": "true", "name": "Asha"}
        if token == "good":
            return httpx.Response(200, json={**base, "aud": "my-client"})
        if token == "other-app":
            return httpx.Response(200, json={**base, "aud": "someone-else"})
        if token == "unverified":
            return httpx.Response(200, json={**base, "aud": "my-client", "email_verified": "false"})
        return httpx.Response(400, json={"error": "invalid_token"})

    g = GoogleAuth(dataclasses.replace(settings, google_client_id="my-client"))
    g._http = httpx.Client(transport=httpx.MockTransport(handler))
    assert g.verify("good") == {"sub": "123", "email": "a@gmail.com", "name": "Asha"}
    for bad in ("other-app", "unverified", "expired"):
        with pytest.raises(GoogleAuthError):
            g.verify(bad)


def test_accounts_db_from_previous_version_upgrades(tmp_path):
    import sqlite3
    from app.accounts import Accounts
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)  # schema used before Google sign-in
    db.executescript("""CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                        password_hash TEXT NOT NULL, balance_paise INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
                        INSERT INTO users VALUES ('u1', 'old@gmail.com', 'Old', 'scrypt$x$y', 2500, '2026-01-01');""")
    db.commit()
    db.close()
    accounts = Accounts(str(path))
    linked = accounts.user_for_google("g-1", "OLD@gmail.com", "Old User")
    assert linked["id"] == "u1" and linked["balance_paise"] == 2500  # existing wallet kept
    new = accounts.user_for_google("g-2", "new@gmail.com", "New")
    assert new["email"] == "new@gmail.com" and new["balance_paise"] == 0
    assert accounts.user_for_google("g-2", "new@gmail.com", "New")["id"] == new["id"]
