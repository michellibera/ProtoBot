import pytest
from fastapi.testclient import TestClient

import core
import web
from test_core import FakeChannel, root  # noqa: F401  (fixture)


@pytest.fixture
def client(root, monkeypatch):  # noqa: F811
    monkeypatch.setattr(web, "WEB_TOKEN", "secret")
    monkeypatch.setattr(core, "start_worker", lambda agent: None)  # keep messages queued for inspection
    return TestClient(web.app)


H = {"X-Token": "secret"}


@pytest.mark.parametrize("headers", [{}, {"X-Token": "wrong"}])
def test_api_requires_token(client, headers):
    assert client.get("/api/agents", headers=headers).status_code == 401


def test_page_is_served_without_token(client):
    r = client.get("/")
    assert r.status_code == 200 and "ProtoBot" in r.text


def test_create_agent_and_send_message(client):
    r = client.post("/api/agents", json={"name": "research", "role": "Papers."}, headers=H)
    assert r.status_code == 201 and r.json()["role"] == "Papers."
    assert [a["name"] for a in client.get("/api/agents", headers=H).json()] == ["main", "research"]

    assert client.post("/api/agents/research/messages", json={"text": "hi"}, headers=H).status_code == 202
    item = core.agents["research"].queue.get_nowait()
    assert item["channel"] == "web" and item["text"] == "hi"
    assert client.get("/api/agents/research/messages", headers=H).json()[0]["text"] == "hi"


def test_create_agent_validation_error_is_400(client):
    r = client.post("/api/agents", json={"name": "main", "role": "x"}, headers=H)
    assert r.status_code == 400


def test_unknown_agent_is_404(client):
    assert client.get("/api/agents/ghost/messages", headers=H).status_code == 404
