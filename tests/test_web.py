from datetime import datetime
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


def test_settings_update_role_model_turns_and_pause(client, root):  # noqa: F811
    tmp, _ = root
    client.post("/api/agents", json={"name": "research", "role": "Papers."}, headers=H)
    r = client.patch("/api/agents/research", json={"role": "Patents.", "model": "opus", "max_turns": 10,
                                                   "paused": True}, headers=H)
    assert r.status_code == 200
    a = r.json()
    assert (a["role"], a["model"], a["max_turns"], a["paused"]) == ("Patents.", "opus", 10, True)
    claude_md = (tmp / "agents" / "research" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Patents." in claude_md and "Papers." not in claude_md and "../../tools/" in claude_md

    a = client.patch("/api/agents/research", json={"model": "", "max_turns": 0, "paused": False}, headers=H).json()
    assert (a["model"], a["max_turns"], a["paused"]) == ("", core.MAX_TURNS, False)


def test_main_role_is_read_only(client):
    assert client.patch("/api/agents/main", json={"role": "x"}, headers=H).status_code == 400
    assert client.patch("/api/agents/main", json={"paused": True}, headers=H).json()["paused"] is True


def test_paused_agent_runs_no_jobs(client, monkeypatch):
    research = core.create_agent("research", "role", start=False)
    core.save_json(research.jobs_file, [{"id": "tick", "schedule": {"type": "once", "at": "2026-01-01T00:00"},
                                         "prompt": "x"}])
    started = []
    monkeypatch.setattr(core.threading, "Thread", lambda target, args, daemon: type(
        "T", (), {"start": lambda self: started.append(args[1]["id"])})())
    client.patch("/api/agents/research", json={"paused": True}, headers=H)
    core.schedule_tick(research, datetime(2026, 10, 30, 9, 0))
    assert started == [] and client.get("/api/agents/research/jobs", headers=H).json()[0]["next_run"] is None

    client.patch("/api/agents/research", json={"paused": False}, headers=H)
    core.schedule_tick(research, datetime(2026, 10, 30, 9, 0))
    assert started == ["tick"]


def test_rules_add_and_delete_keep_other_lines(client):
    rules = core.agents["main"].memory_dir / "rules.md"
    rules.write_text("# Rules\n\nIntro text.\n- Be brief.\n", encoding="utf-8")
    assert client.get("/api/agents/main/setup", headers=H).json()["rules"] == ["Be brief."]

    assert client.post("/api/agents/main/rules", json={"text": "Cite\nsources."}, headers=H).json() \
        == ["Be brief.", "Cite sources."]
    assert client.post("/api/agents/main/rules/delete", json={"text": "Be brief."}, headers=H).json() \
        == ["Cite sources."]
    assert rules.read_text(encoding="utf-8") == "# Rules\n\nIntro text.\n- Cite sources.\n"
    assert client.post("/api/agents/main/rules/delete", json={"text": "gone"}, headers=H).status_code == 404
    assert client.post("/api/agents/main/rules", json={"text": "  "}, headers=H).status_code == 400


def test_memory_and_knowledge_files_are_listed_and_readable(client, root):  # noqa: F811
    tmp, _ = root
    mem = core.agents["main"].memory_dir
    (mem / "knowledge").mkdir()
    (mem / "knowledge" / "market.md").write_text("Prices.", encoding="utf-8")
    (tmp / "agent" / "secret.txt").write_text("no", encoding="utf-8")

    s = client.get("/api/agents/main/setup", headers=H).json()
    assert [f["path"] for f in s["knowledge"]] == ["knowledge/market.md"]
    assert "MEMORY.md" in [f["path"] for f in s["memory"]]
    assert client.get("/api/agents/main/memory", params={"path": "knowledge/market.md"},
                      headers=H).json()["text"] == "Prices."
    for bad in ("../secret.txt", str(tmp / "agent" / "secret.txt"), "knowledge"):
        assert client.get("/api/agents/main/memory", params={"path": bad}, headers=H).status_code == 404


JOB = {"id": "digest", "schedule": {"type": "daily", "time": "8:00", "days": [4, 0, 0]}, "prompt": " Scan. "}


def test_job_create_edit_delete(client):
    r = client.post("/api/agents/main/jobs", json=JOB, headers=H)
    assert r.status_code == 201
    assert r.json() == {"id": "digest", "enabled": True, "prompt": "Scan.",
                        "schedule": {"type": "daily", "time": "08:00", "days": [0, 4]}}
    assert client.post("/api/agents/main/jobs", json=JOB, headers=H).status_code == 400  # duplicate id

    jobs_file = core.agents["main"].jobs_file
    core.save_json(jobs_file, [dict(core.load_json(jobs_file, [])[0], chat_id=7)])  # a key the UI does not know
    r = client.put("/api/agents/main/jobs/digest", headers=H, json={
        "id": "digest", "enabled": False, "schedule": {"type": "every", "minutes": 30}, "prompt": "Other."})
    assert r.status_code == 200
    [saved] = core.load_json(jobs_file, [])
    assert saved == {"id": "digest", "enabled": False, "schedule": {"type": "every", "minutes": 30},
                     "prompt": "Other.", "chat_id": 7}

    assert client.put("/api/agents/main/jobs/ghost", json=JOB, headers=H).status_code == 404
    assert client.delete("/api/agents/main/jobs/digest", headers=H).status_code == 200
    assert core.load_json(jobs_file, None) == []
    assert client.delete("/api/agents/main/jobs/digest", headers=H).status_code == 404


@pytest.mark.parametrize("bad", [
    {"id": "Bad Id"}, {"prompt": " "}, {"schedule": {"type": "daily", "time": "25:00"}},
    {"schedule": {"type": "daily", "time": "08:00", "days": [7]}}, {"schedule": {"type": "every", "minutes": 0}},
    {"schedule": {"type": "once", "at": "tomorrow"}}, {"schedule": {"type": "cron"}},
])
def test_job_validation(client, bad):
    assert client.post("/api/agents/main/jobs", json={**JOB, **bad}, headers=H).status_code == 400


def test_run_now_starts_job_without_touching_last_run(client, monkeypatch):
    client.post("/api/agents/main/jobs", json=JOB, headers=H)
    started = []
    monkeypatch.setattr(core.threading, "Thread", lambda target, args, daemon: type(
        "T", (), {"start": lambda self: started.append(args[1]["id"])})())
    assert client.post("/api/agents/main/jobs/digest/run", headers=H).status_code == 202
    assert started == ["digest"] and core.agents["main"].running_jobs == {"digest"}
    assert client.get("/api/agents/main/jobs", headers=H).json()[0]["running"] is True
    assert client.post("/api/agents/main/jobs/digest/run", headers=H).status_code == 409  # already running
    assert client.post("/api/agents/main/jobs/ghost/run", headers=H).status_code == 404
    assert core.load_json(core.agents["main"].jobs_state_file, {}) == {}


def test_status_counts_jobs(client):
    client.post("/api/agents/main/jobs", json=JOB, headers=H)
    assert client.get("/api/agents", headers=H).json()[0]["job_count"] == 1
