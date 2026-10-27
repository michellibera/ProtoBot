import json
from datetime import datetime
from pathlib import Path

import pytest

import core


class FakeChannel:
    label = "Test"

    def __init__(self):
        self.delivered = []

    def prompt_suffix(self, item):
        return ""

    def busy(self, item):
        return core.nullcontext()

    def deliver(self, agent, item, text):
        self.delivered.append((agent.name, text))
        return text


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A minimal copy of the repo layout: agent/ with the template files new agents are made from."""
    main = tmp_path / "agent"
    (main / "memory.template").mkdir(parents=True)
    (main / "memory.template" / "MEMORY.md").write_text("# index", encoding="utf-8")
    (main / ".claude" / "skills" / "schedule-job").mkdir(parents=True)
    (main / ".claude" / "skills" / "schedule-job" / "SKILL.md").write_text("skill", encoding="utf-8")
    (main / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (main / "CLAUDE.md").write_text("Send files: python ../tools/tg_send.py", encoding="utf-8")

    core.set_root(tmp_path)
    monkeypatch.setattr(core, "agents", {})
    monkeypatch.setattr(core, "_seq", 0)
    monkeypatch.setattr(core, "channels", {"test": FakeChannel(), "web": core.WebChannel()})
    sent = []
    monkeypatch.setattr(core, "notifier", lambda text, chat_id=None: sent.append(text) or chat_id or 42)
    core.init()
    yield tmp_path, sent
    core.set_root(Path(core.__file__).resolve().parent)


@pytest.fixture
def fake_claude(monkeypatch):
    calls = []

    def run(agent, prompt, session_id=None, track=True):
        calls.append({"agent": agent.name, "cwd": agent.path, "prompt": prompt, "session_id": session_id})
        return {"text": f"reply from {agent.name}", "session_id": f"s-{agent.name}", "is_error": False, "cost": 0}

    monkeypatch.setattr(core, "run_claude", run)
    return calls


def drain(agent):
    while not agent.queue.empty():
        core.process(agent, agent.queue.get())


@pytest.mark.parametrize("name", ["main", "Bad", "x", "a/b", "../etc", "_template"])
def test_create_agent_rejects_invalid_or_taken_names(root, name):
    with pytest.raises(ValueError):
        core.create_agent(name, "role", start=False)


def test_create_agent_builds_own_workspace_from_template(root):
    tmp, _ = root
    agent = core.create_agent("research", "Finds and summarises papers.", start=False)

    assert agent.path == tmp / "agents" / "research"
    assert (agent.path / "memory" / "MEMORY.md").exists()
    assert (agent.path / ".claude" / "skills" / "schedule-job" / "SKILL.md").exists()
    claude_md = (agent.path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Finds and summarises papers." in claude_md
    assert "../../tools/tg_send.py" in claude_md  # tools are two levels up from agents/<name>/
    assert json.loads((agent.path / "jobs.json").read_text()) == []


def test_messages_run_in_their_agent_directory_and_reply_to_their_channel(root, fake_claude):
    core.create_agent("research", "role", start=False)
    core.submit("research", "hello", "test")
    core.submit("main", "hi main", "test")
    drain(core.agents["research"])
    drain(core.agents["main"])

    assert [c["cwd"].name for c in fake_claude] == ["research", "agent"]
    assert core.channels["test"].delivered == [("research", "reply from research"), ("main", "reply from main")]
    chat = core.agents["research"].read_chat()
    assert [(m["role"], m["text"]) for m in chat] == [("user", "hello"), ("agent", "reply from research")]
    assert core.agents["research"].get_state()["session_id"] == "s-research"


def test_session_is_resumed_per_agent(root, fake_claude):
    core.submit("main", "one", "test")
    core.submit("main", "two", "test")
    drain(core.agents["main"])

    assert [c["session_id"] for c in fake_claude] == [None, "s-main"]


def test_crash_replays_only_unprocessed_messages_per_agent(root, fake_claude, monkeypatch):
    core.create_agent("research", "role", start=False)
    core.submit("main", "done", "test")
    core.submit("research", "pending research", "test")
    core.submit("main", "pending main", "test")
    core.process(core.agents["main"], core.agents["main"].queue.get())  # only the first one finished

    # restart: fresh registry, same files
    monkeypatch.setattr(core, "agents", {})
    core.init()

    assert [i["text"] for i in list(core.agents["main"].queue.queue)] == ["pending main"]
    assert [i["text"] for i in list(core.agents["research"].queue.queue)] == ["pending research"]


def test_legacy_single_agent_state_is_migrated(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "state.json").write_text(json.dumps({"offset": 5, "processed_id": 10, "session_id": "old", "turns": 3}))
    (state / "inbox.jsonl").write_text(
        json.dumps({"id": 10, "chat_id": 1, "text": "processed"}) + "\n"
        + json.dumps({"id": 11, "chat_id": 1, "text": "lost in crash"}) + "\n"
    )
    (state / "jobs_state.json").write_text(json.dumps({"brief": {"last_run": "2026-10-01T08:00:00"}}))
    (tmp_path / "agent" / "memory.template").mkdir(parents=True)
    core.set_root(tmp_path)
    monkeypatch.setattr(core, "agents", {})

    pending = core.init()

    main = core.agents["main"]
    assert [i["text"] for i in pending] == ["lost in crash"]
    assert main.get_state()["session_id"] == "old" and main.get_state()["turns"] == 3
    assert json.loads(main.jobs_state_file.read_text())["brief"]["last_run"] == "2026-10-01T08:00:00"
    assert (state / "inbox.legacy.jsonl").exists() and not (state / "inbox.jsonl").exists()
    core.set_root(Path(core.__file__).resolve().parent)


def test_job_report_main_full_on_telegram_others_short_with_ui_pointer(root):
    _, sent = root
    research = core.create_agent("research", "role", start=False)
    long_text = "x" * 1000

    core.report_job(core.agents["main"], {"id": "brief"}, "short report")
    core.report_job(research, {"id": "scan"}, long_text)

    assert sent[0] == "[brief] short report"
    assert sent[1].startswith("[research/scan] ") and "full report in the web UI" in sent[1]
    assert len(sent[1]) < 400
    assert research.read_chat()[-1]["text"] == f"[scan] {long_text}"


def test_main_job_reports_are_logged_as_sent_questions_in_the_v1_format(root):
    tmp, _ = root
    core.report_job(core.agents["main"], {"id": "checkin"}, "Did you pay the rent?")
    core.report_job(core.agents["main"], {"id": "other", "chat_id": 7}, "Second")

    records = core.read_jsonl(tmp / "state" / "sent_questions.jsonl")
    assert [{k: r[k] for k in ("job_id", "chat_id", "text")} for r in records] == [
        {"job_id": "checkin", "chat_id": 42, "text": "[checkin] Did you pay the rent?"},
        {"job_id": "other", "chat_id": 7, "text": "[other] Second"},
    ]
    assert set(records[0]) == {"ts", "job_id", "chat_id", "text"}


def test_unsent_or_other_agent_reports_are_not_logged_as_sent_questions(root, monkeypatch):
    tmp, _ = root
    core.report_job(core.create_agent("research", "role", start=False), {"id": "scan"}, "found 3 papers")
    monkeypatch.setattr(core, "notifier", None)  # no Telegram: nothing was sent
    core.report_job(core.agents["main"], {"id": "checkin"}, "question")

    assert not (tmp / "state" / "sent_questions.jsonl").exists()


def test_new_command_rotates_session(root, fake_claude):
    main = core.agents["main"]
    main.update_state(session_id="abc", turns=7)
    core.submit("main", "/new", "test")
    drain(main)

    assert fake_claude[0]["prompt"] == core.CHECKPOINT_PROMPT
    assert main.get_state()["session_id"] is None and main.get_state()["turns"] == 0


@pytest.mark.parametrize("now,last,expected", [
    ("2026-10-27 08:05", None, True),
    ("2026-10-27 07:55", None, False),
    ("2026-10-27 08:05", "2026-10-27 08:00", False),
    ("2026-10-27 15:00", None, False),  # more than 6 h late: skipped
])
def test_daily_schedule(now, last, expected):
    job = {"schedule": {"type": "daily", "time": "08:00"}}
    parse = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M") if s else None
    assert core.is_due(job, parse(last), parse(now)) is expected
