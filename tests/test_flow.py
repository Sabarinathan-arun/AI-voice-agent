import io
import json
from datetime import timedelta

from openpyxl import load_workbook

from app.store import utcnow
from app.worker import parse_execution

SETUP = {"business_name": "Acme Solar", "product_details": "Rooftop solar, 3kW from 1.5L",
         "sales_instructions": "Book a free site visit.", "language": "Tamil",
         "welcome_message": "Vanakkam {customer_name}"}
CSV = b"name,phone,City,Last Purchase\nAsha,9876543210,Chennai,Inverter\nRavi,9876543211,Madurai,\nMeena,9876543212,Salem,Panels\n"


def paid_campaign(client, payments):
    res = client.post("/api/campaigns")
    assert res.status_code == 201
    cid = res.json()["campaign_id"]
    assert res.json()["checkout_url"].startswith("https://rzp.io/")
    # not paid yet: verify does nothing and setup is locked
    assert client.post(f"/api/campaigns/{cid}/verify-payment").json()["status"] == "pending_payment"
    assert client.put(f"/api/campaigns/{cid}/setup", json=SETUP).status_code == 409
    payments.paid.add(cid)
    assert client.post(f"/api/campaigns/{cid}/verify-payment").json()["status"] == "paid"
    return cid


def test_full_workflow(client, bolna, payments):
    cid = paid_campaign(client, payments)
    assert client.post(f"/api/campaigns/{cid}/start").status_code == 422  # nothing set up yet

    assert client.put(f"/api/campaigns/{cid}/setup", json=SETUP).status_code == 200
    res = client.post(f"/api/campaigns/{cid}/leads", files={"file": ("leads.csv", CSV, "text/csv")})
    assert res.json()["imported"] == 3
    assert res.json()["detail_columns"] == ["City", "Last Purchase"]

    res = client.post(f"/api/campaigns/{cid}/start")
    assert res.status_code == 200 and res.json()["campaign"]["status"] == "running"
    assert "bolna_agent_id" not in res.json()["campaign"]
    agent_campaign = bolna.agents[0]  # the agent is created from what the customer entered
    assert agent_campaign["language"] == "Tamil" and agent_campaign["welcome_message"] == "Vanakkam {customer_name}"
    assert client.put(f"/api/campaigns/{cid}/setup", json=SETUP).status_code == 409

    client.worker.tick()
    assert len(bolna.calls) == 2  # concurrency limit
    agent_id, phone, data = bolna.calls[0]
    assert agent_id == "agent-1" and phone == "+919876543210"
    assert data == {"customer_name": "Asha", "customer_details": "City: Chennai; Last Purchase: Inverter",
                    "city": "Chennai", "last_purchase": "Inverter"}
    assert bolna.calls[1][2]["customer_details"] == "City: Madurai"

    bolna.executions["exec-0"] = {
        "id": "exec-0", "status": "completed", "transcript": "assistant: hi\nuser: yes please",
        "summary": "Asha booked a site visit.", "extracted_data": {"outcome": "Interested"},
        "conversation_duration": 42, "telephony_data": {"recording_url": "https://rec/0"},
    }
    bolna.executions["exec-1"] = {"id": "exec-1", "status": "no-answer"}
    client.worker.tick()
    assert len(bolna.calls) == 3

    # webhook path: payload id only, data re-fetched from Bolna
    bolna.executions["exec-2"] = {"id": "exec-2", "status": "completed", "transcript": "assistant: hello"}
    assert client.post("/api/webhooks/bolna", json={"id": "exec-2", "status": "forged"}).status_code == 200
    client.worker.tick()

    data = client.get(f"/api/campaigns/{cid}").json()
    assert data["campaign"]["status"] == "completed"
    by_name = {l["name"]: l for l in data["leads"]}
    assert by_name["Asha"]["outcome"] == "interested"
    assert by_name["Asha"]["summary"] == "Asha booked a site visit."
    assert by_name["Ravi"]["outcome"] == "no_answer"
    assert by_name["Meena"]["outcome"] == "no_conversation"
    assert data["stats"]["done"] == 3
    assert bolna.deleted == ["agent-1"]  # agent removed once calling finished

    res = client.get(f"/api/campaigns/{cid}/export")
    assert res.status_code == 200
    rows = list(load_workbook(io.BytesIO(res.content)).active.iter_rows(values_only=True))
    assert rows[0][:7] == ("Name", "Phone", "City", "Last Purchase", "Call status", "Outcome", "Summary")
    assert rows[1][:6] == ("Asha", "+919876543210", "Chennai", "Inverter", "completed", "interested")


def test_stop_skips_pending(client, bolna, payments):
    cid = paid_campaign(client, payments)
    client.put(f"/api/campaigns/{cid}/setup", json=SETUP)
    client.post(f"/api/campaigns/{cid}/leads", files={"file": ("leads.csv", CSV, "text/csv")})
    client.post(f"/api/campaigns/{cid}/start")
    client.worker.tick()
    assert client.post(f"/api/campaigns/{cid}/stop").json()["campaign"]["status"] == "stopped"
    for eid in ("exec-0", "exec-1"):
        bolna.executions[eid] = {"id": eid, "status": "busy"}
    client.worker.tick()
    leads = client.get(f"/api/campaigns/{cid}").json()["leads"]
    assert len(bolna.calls) == 2
    assert [l["status"] for l in leads] == ["busy", "busy", "skipped"]
    assert bolna.deleted == ["agent-1"]


def test_unknown_language_rejected(client, payments):
    cid = paid_campaign(client, payments)
    assert client.put(f"/api/campaigns/{cid}/setup", json={**SETUP, "language": "Klingon"}).status_code == 422


def test_call_timeout(client, bolna, payments):
    cid = paid_campaign(client, payments)
    client.put(f"/api/campaigns/{cid}/setup", json=SETUP)
    client.post(f"/api/campaigns/{cid}/leads", files={"file": ("l.csv", b"name,phone\nA,9876543210\n", "text/csv")})
    client.post(f"/api/campaigns/{cid}/start")
    client.worker.tick()
    client.worker.tick(utcnow() + timedelta(hours=2))
    data = client.get(f"/api/campaigns/{cid}").json()
    assert data["leads"][0]["status"] == "timed_out"
    assert data["campaign"]["status"] == "completed"


def test_nothing_is_kept_after_idle_session(client, payments):
    cid = paid_campaign(client, payments)
    client.worker.tick(utcnow() + timedelta(hours=25))
    assert client.get(f"/api/campaigns/{cid}").status_code == 404


def test_parse_execution_variants():
    f = parse_execution({"status": "completed", "transcript": [{"role": "user", "content": "hi"}],
                         "extracted_data": json.dumps({"outcome": {"value": "callback requested"}})})
    assert f["outcome"] == "callback_requested" and f["transcript"] == "user: hi" and f["summary"] == "user: hi"
    assert parse_execution({"status": "scheduled"})["status"] == "in-progress"
    assert "outcome" not in parse_execution({"status": "ringing"})
    assert "outcome" not in parse_execution({"status": "call-disconnected"})  # wait for 'completed'


def test_parse_execution_nested_extraction():
    # shape from Bolna's API quickstart: extracted data grouped by category
    f = parse_execution({
        "status": "completed", "conversation_duration": 16, "transcript": "assistant: hi\nuser: hello",
        "extracted_data": {"General": {"Call Summary": {"subjective": "Customer wants a demo."},
                                       "Outcome": {"value": "Interested"}}},
        "telephony_data": {"duration": 16, "recording_url": "https://api.bolna.ai/recordings/call/x",
                           "hangup_reason": "inactivity_timeout"},
    })
    assert f["summary"] == "Customer wants a demo."
    assert f["outcome"] == "interested"
    assert f["duration_seconds"] == 16 and f["recording_url"].endswith("/x")
