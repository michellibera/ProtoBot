from pathlib import Path

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


def test_delete_agent_moves_its_files_to_deleted(client, root):  # noqa: F811
    tmp, _ = root
    client.post("/api/agents", json={"name": "research", "role": "Papers."}, headers=H)
    client.post("/api/agents/research/messages", json={"text": "hi"}, headers=H)

    assert client.delete("/api/agents/research", headers=H).status_code == 200
    assert [a["name"] for a in client.get("/api/agents", headers=H).json()] == ["main"]
    assert not (tmp / "agents" / "research").exists()
    [dest] = (tmp / "deleted").iterdir()
    assert (dest / "workspace" / "CLAUDE.md").exists() and (dest / "state" / "chat.jsonl").exists()


def test_delete_refuses_main_and_working_agents(client):
    assert client.delete("/api/agents/main", headers=H).status_code == 409
    client.post("/api/agents", json={"name": "research", "role": "Papers."}, headers=H)
    core.agents["research"].running_jobs.add("scan")
    assert client.delete("/api/agents/research", headers=H).status_code == 409
    assert client.delete("/api/agents/ghost", headers=H).status_code == 404


def test_failed_file_move_keeps_the_agent_intact(client, root, monkeypatch):  # noqa: F811
    tmp, _ = root
    client.post("/api/agents", json={"name": "research", "role": "Papers."}, headers=H)
    client.post("/api/agents/research/messages", json={"text": "hi"}, headers=H)
    real_replace = core.os.replace

    def locked_state(src, dst):  # workspace moves, state is "in use"
        if Path(src) == core.agents["research"].state_dir:
            raise PermissionError("file in use")
        real_replace(src, dst)

    monkeypatch.setattr(core.os, "replace", locked_state)
    r = client.delete("/api/agents/research", headers=H)

    assert r.status_code == 409 and "in use" in r.json()["detail"]
    assert "research" in core.agents
    assert (tmp / "agents" / "research" / "CLAUDE.md").exists()  # workspace moved back


def test_recreated_agent_does_not_replay_messages_of_the_deleted_one(client, monkeypatch):
    client.post("/api/agents", json={"name": "research", "role": "Papers."}, headers=H)
    client.post("/api/agents/research/messages", json={"text": "old"}, headers=H)
    client.delete("/api/agents/research", headers=H)
    client.post("/api/agents", json={"name": "research", "role": "Again."}, headers=H)

    monkeypatch.setattr(core, "agents", {})
    core.init()  # restart
    assert core.agents["research"].queue.empty()


def test_jobs_lists_saved_jobs_with_last_run(client):
    research = core.create_agent("research", "role", start=False)
    core.save_json(research.jobs_file, [
        {"id": "scan", "schedule": {"type": "daily", "time": "08:00"}, "prompt": "Scan."},
        {"id": "old", "enabled": False, "schedule": {"type": "every", "minutes": 60}, "prompt": "x"},
    ])
    core.save_json(research.jobs_state_file, {"scan": {"last_run": "2026-10-29T08:00:00"}})

    jobs = client.get("/api/agents/research/jobs", headers=H).json()
    assert [(j["id"], j["last_run"]) for j in jobs] == [("scan", "2026-10-29T08:00:00"), ("old", None)]


def test_jobs_tolerates_a_broken_jobs_file(client):
    core.save_json(core.agents["main"].jobs_file, {"not": "a list"})
    assert client.get("/api/agents/main/jobs", headers=H).json() == []


def test_unknown_agent_is_404(client):
    assert client.get("/api/agents/ghost/messages", headers=H).status_code == 404
