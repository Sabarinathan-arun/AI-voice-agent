import dataclasses

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app


class FakeBolna:
    def __init__(self):
        self.agents, self.deleted, self.calls, self.executions = [], [], [], {}

    def create_agent(self, campaign, webhook_url):
        self.agents.append(campaign["id"])
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


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PAYMENT_PROVIDER", "mock")
    monkeypatch.setenv("BOLNA_MOCK", "true")
    return dataclasses.replace(load_settings(), max_concurrent_calls=2, app_base_url="http://testserver")


@pytest.fixture
def bolna():
    return FakeBolna()


@pytest.fixture
def client(settings, bolna):
    app = create_app(settings, bolna=bolna, run_worker=False)
    with TestClient(app) as c:
        c.worker = app.state.worker
        c.db = app.state.db
        yield c
