import dataclasses

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app


class FakeBolna:
    """Stands in for Bolna in tests only; the app itself always talks to the real API."""

    def __init__(self):
        self.agents, self.deleted, self.calls, self.executions = [], [], [], {}

    def create_agent(self, campaign, webhook_url, voice=None):
        self.agents.append({**campaign, "_voice": voice})
        return "agent-1"

    def delete_agent(self, agent_id):
        self.deleted.append(agent_id)

    def start_call(self, agent_id, phone, user_data):
        execution_id = f"exec-{len(self.calls)}"
        self.calls.append((agent_id, phone, user_data))
        self.executions[execution_id] = {"id": execution_id, "status": "ringing"}
        return execution_id

    def get_execution(self, execution_id):
        return self.executions[execution_id]


class FakeRazorpay:
    def __init__(self):
        self.paid: set[str] = set()   # reference ids marked as paid
        self.links: list[dict] = []

    def create_checkout(self, reference_id, amount_paise, description, callback_url):
        self.links.append({"ref": reference_id, "amount": amount_paise, "callback": callback_url})
        return f"https://rzp.io/i/{reference_id}", f"plink_{reference_id}"

    def is_paid(self, payment_link_id, reference_id):
        return payment_link_id == f"plink_{reference_id}" and reference_id in self.paid

    def pay_all(self):
        self.paid.update(link["ref"] for link in self.links)


class FakeGoogle:
    """Stands in for Google sign-in: a credential "google:<email>" is a verified Google account."""

    def verify(self, credential):
        from app.google_auth import GoogleAuthError
        if not credential.startswith("google:"):
            raise GoogleAuthError("Google sign-in failed. Please try again.")
        email = credential.split(":", 1)[1]
        return {"sub": "sub-" + email, "email": email, "name": email.split("@")[0].title()}


@pytest.fixture
def settings(tmp_path):
    return dataclasses.replace(load_settings(), max_concurrent_calls=2, app_base_url="http://testserver",
                               database_path=str(tmp_path / "accounts.db"), price_per_minute_paise=1000,
                               min_topup_paise=10000, low_balance_paise=2000, google_client_id="test-client")


@pytest.fixture
def bolna():
    return FakeBolna()


@pytest.fixture
def payments():
    return FakeRazorpay()


@pytest.fixture
def client(settings, bolna, payments):
    app = create_app(settings, bolna=bolna, payments=payments, google=FakeGoogle(), run_worker=False)
    with TestClient(app) as c:
        c.worker = app.state.worker
        c.store = app.state.store
        c.accounts = app.state.accounts
        yield c


def signup(client, email="owner@example.com"):
    res = client.post("/api/auth/google", json={"credential": f"google:{email}"})
    assert res.status_code == 200, res.text
    return res.json()


def top_up(client, payments, rupees=500):
    assert client.post("/api/wallet/topups", json={"amount_rupees": rupees}).status_code == 201
    payments.pay_all()
    return client.post("/api/wallet/topups/verify").json()
