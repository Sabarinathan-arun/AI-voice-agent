import io
import json
from datetime import timedelta

from openpyxl import load_workbook

from app.db import utcnow
from app.worker import parse_execution

SETUP = {"business_name": "Acme Solar", "product_details": "Rooftop solar, 3kW from 1.5L",
         "sales_instructions": "Book a free site visit."}
CSV = b"name,phone\nAsha,9876543210\nRavi,9876543211\nMeena,9876543212\n"


def paid_campaign(client):
    res = client.post("/api/campaigns")
    assert res.status_code == 201
    cid = res.json()["campaign_id"]
    assert client.get(f"/api/campaigns/{cid}").json()["campaign"]["status"] == "pending_payment"
    # setup is locked until paid
    assert client.put(f"/api/campaigns/{cid}/setup", json=SETUP).status_code == 409
    res = client.get(res.json()["checkout_url"].replace("http://testserver", ""), follow_redirects=False)
    assert res.status_code == 303
    return cid


def test_full_workflow(client, bolna):
    cid = paid_campaign(client)
    assert client.post(f"/api/campaigns/{cid}/start").status_code == 422  # nothing set up yet

    assert client.put(f"/api/campaigns/{cid}/setup", json=SETUP).status_code == 200
    res = client.post(f"/api/campaigns/{cid}/leads", files={"file": ("leads.csv", CSV, "text/csv")})
    assert res.json()["imported"] == 3

    res = client.post(f"/api/campaigns/{cid}/start")
    assert res.status_code == 200 and res.json()["campaign"]["status"] == "running"
    assert "bolna_agent_id" not in res.json()["campaign"]
    # setup is locked once running
    assert client.put(f"/api/campaigns/{cid}/setup", json=SETUP).status_code == 409

    client.worker.tick()
    assert len(bolna.calls) == 2  # concurrency limit
    assert bolna.calls[0][2] == {"customer_name": "Asha", "business_name": "Acme Solar"}

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

    res = client.get(f"/api/campaigns/{cid}/export")
    assert res.status_code == 200
    ws = load_workbook(io.BytesIO(res.content)).active
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0][:4] == ("Name", "Phone", "Call status", "Outcome")
    assert rows[1][0] == "Asha" and rows[1][3] == "interested"


def test_stop_skips_pending(client, bolna):
    cid = paid_campaign(client)
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


def test_call_timeout(client, bolna):
    cid = paid_campaign(client)
    client.put(f"/api/campaigns/{cid}/setup", json=SETUP)
    client.post(f"/api/campaigns/{cid}/leads", files={"file": ("l.csv", b"name,phone\nA,9876543210\n", "text/csv")})
    client.post(f"/api/campaigns/{cid}/start")
    client.worker.tick()
    client.worker.tick(utcnow() + timedelta(hours=2))
    data = client.get(f"/api/campaigns/{cid}").json()
    assert data["leads"][0]["status"] == "timed_out"
    assert data["campaign"]["status"] == "completed"


def test_automatic_deletion(client, bolna, settings):
    cid = paid_campaign(client)
    client.put(f"/api/campaigns/{cid}/setup", json=SETUP)
    client.post(f"/api/campaigns/{cid}/leads", files={"file": ("leads.csv", CSV, "text/csv")})
    client.post(f"/api/campaigns/{cid}/start")
    client.worker.tick(utcnow() + timedelta(hours=settings.retention_hours - 1))
    assert client.get(f"/api/campaigns/{cid}").status_code == 200
    client.worker.cleanup(utcnow() + timedelta(hours=settings.retention_hours + 1))
    assert client.get(f"/api/campaigns/{cid}").status_code == 404
    assert client.db.get_leads(cid) == []
    assert bolna.deleted == ["agent-1"]


def test_unpaid_campaign_deleted(client, settings):
    cid = client.post("/api/campaigns").json()["campaign_id"]
    client.worker.cleanup(utcnow() + timedelta(hours=settings.unpaid_retention_hours + 1))
    assert client.get(f"/api/campaigns/{cid}").status_code == 404


def test_manual_delete(client):
    cid = paid_campaign(client)
    assert client.delete(f"/api/campaigns/{cid}").status_code == 204
    assert client.get(f"/api/campaigns/{cid}").status_code == 404


def test_parse_execution_variants():
    f = parse_execution({"status": "completed", "transcript": [{"role": "user", "content": "hi"}],
                         "extracted_data": json.dumps({"outcome": {"value": "callback requested"}})})
    assert f["outcome"] == "callback_requested" and f["transcript"] == "user: hi" and f["summary"] == "user: hi"
    assert parse_execution({"status": "scheduled"})["status"] == "in-progress"
    assert "outcome" not in parse_execution({"status": "ringing"})
