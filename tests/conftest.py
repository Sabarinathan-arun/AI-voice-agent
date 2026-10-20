import dataclasses

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app


class FakeBolna:
    """Stands in for Bolna in tests only; the app itself always talks to the real API."""

    def __init__(self):
        self.agents, self.deleted, self.calls, self.executions = [], [], [], {}

    def create_agent(self, campaign, webhook_url):
        self.agents.append(campaign)
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
        self.paid: set[str] = set()

    def create_checkout(self, campaign_id):
        return f"https://rzp.io/i/{campaign_id}", f"plink_{campaign_id}"

    def is_paid(self, payment_link_id, campaign_id):
        return payment_link_id == f"plink_{campaign_id}" and campaign_id in self.paid


@pytest.fixture
def settings():
    return dataclasses.replace(load_settings(), max_concurrent_calls=2, app_base_url="http://testserver")


@pytest.fixture
def bolna():
    return FakeBolna()


@pytest.fixture
def payments():
    return FakeRazorpay()


@pytest.fixture
def client(settings, bolna, payments):
    app = create_app(settings, bolna=bolna, payments=payments, run_worker=False)
    with TestClient(app) as c:
        c.worker = app.state.worker
        c.store = app.state.store
        yield c
