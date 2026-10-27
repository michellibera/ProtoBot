"""
ProtoBot core: agents, per-agent queues and sessions, scheduler, crash-safe inbox.

An agent is a working directory for Claude Code (its role in CLAUDE.md, memory/, jobs.json, skills).
`main` lives in agent/ (the original single-agent bot), extra agents in agents/<name>/.
Channels (Telegram, web) only submit messages and deliver replies; they never call Claude Code.
State is kept in plain files (state/), no database.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

MAIN = "main"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")

CLAUDE_BIN = os.getenv("CLAUDE_BIN") or shutil.which("claude")
MODEL = os.getenv("CLAUDE_MODEL", "").strip()
PERMISSION_MODE = os.getenv("CLAUDE_PERMISSION_MODE", "bypassPermissions")
TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "1800"))
MAX_TURNS = int(os.getenv("SESSION_MAX_TURNS", "40"))
MAX_PARALLEL_JOBS = int(os.getenv("MAX_PARALLEL_JOBS", "2"))
MAX_PARALLEL_CHATS = int(os.getenv("MAX_PARALLEL_CHATS", "3"))
NOTIFY_PREVIEW_CHARS = 300

CHECKPOINT_PROMPT = (
    "This session is about to be closed (context reset). Save your state to files now: "
    "update memory/handoff.md (current goals, agreements with the user, task statuses, "
    "pending questions, next steps), add durable facts to memory/ and update MEMORY.md. "
    "Reply with one word: OK."
)
JOB_HEADER = (
    "[Scheduled run | job: {jid} | {now}]\n"
    "New session with no chat history. Start by reading memory/MEMORY.md and memory/handoff.md. "
    "Do the task below. Finish with a short report for the user. "
    "If there is nothing worth reporting, reply with exactly: NO_REPORT\n\nTASK:\n{prompt}"
)
ROLE_HEADER = """# My role: {name}
{role}

## How I differ from the shared rules below
- The user talks to me through the ProtoBot web UI, not the Telegram chat. Telegram is only a
  notification channel: `python ../../tools/tg_send.py "text"` (my messages are prefixed with my name).
- My working directory is this folder (agents/{name}/). memory/, jobs.json and skills here are mine.
  Other folders in ../ belong to other agents: I do not read or write them unless the user asks.

---

"""


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# --- paths (set_root lets tests run against a temporary copy) -----------------
MAIN_DIR = AGENTS_DIR = STATE_DIR = INBOX_FILE = ROOT


def set_root(root: Path) -> None:
    global ROOT, MAIN_DIR, AGENTS_DIR, STATE_DIR, INBOX_FILE
    ROOT = root
    MAIN_DIR = root / "agent"
    AGENTS_DIR = root / "agents"
    STATE_DIR = root / "state"
    INBOX_FILE = STATE_DIR / "inbox.jsonl"


set_root(ROOT)


# --- small file helpers -------------------------------------------------------
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


# --- channels -------------------------------------------------------------------
class Channel(Protocol):
    label: str  # shown to the agent in the prompt header, e.g. "Telegram"

    def prompt_suffix(self, item: dict) -> str: ...
    def busy(self, item: dict): ...  # context manager shown while the agent works (e.g. "typing")
    def deliver(self, agent: "Agent", item: dict, text: str) -> str: ...  # returns text for chat history


class WebChannel:
    """Replies are read by the web UI from the chat history, so delivery only records them."""
    label = "Web UI"

    def prompt_suffix(self, item: dict) -> str:
        return ""

    def busy(self, item: dict):
        return nullcontext()

    def deliver(self, agent: "Agent", item: dict, text: str) -> str:
        return text


channels: dict[str, Channel] = {"web": WebChannel()}
# Set by the Telegram channel: (text, chat_id or None for the default chat) -> chat_id it went to, None if not sent.
notifier: Callable[[str, int | None], int | None] | None = None


def notify(text: str, chat_id: int | None = None) -> int | None:
    return notifier(text, chat_id) if notifier else None


# --- agents ---------------------------------------------------------------------
class Agent:
    def __init__(self, name: str, path: Path):
        self.name = name
        self.path = path
        self.state_dir = STATE_DIR / "agents" / name
        self.queue: "queue.Queue[dict]" = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self.stopped = False
        self.running_jobs: set[str] = set()
        self._lock = threading.Lock()

    @property
    def jobs_file(self) -> Path:
        return self.path / "jobs.json"

    @property
    def jobs_state_file(self) -> Path:
        return self.state_dir / "jobs_state.json"

    @property
    def chat_file(self) -> Path:
        return self.state_dir / "chat.jsonl"

    @property
    def meta(self) -> dict:
        return load_json(self.path / "agent.json", {})

    def get_state(self) -> dict:
        with self._lock:
            st = load_json(self.state_dir / "session.json", {})
        st.setdefault("session_id", None)
        st.setdefault("turns", 0)
        st.setdefault("processed_seq", 0)
        return st

    def update_state(self, **changes) -> None:
        with self._lock:
            st = load_json(self.state_dir / "session.json", {})
            st.update(changes)
            save_json(self.state_dir / "session.json", st)

    def log_chat(self, role: str, text: str, channel: str) -> None:
        with self._lock:
            append_jsonl(self.chat_file, {"ts": datetime.now().isoformat(timespec="seconds"),
                                          "role": role, "text": text, "channel": channel})

    def read_chat(self, after: int = 0) -> list[dict]:
        """Chat history entries with index > `after` (1-based index in the file)."""
        return [dict(e, i=i) for i, e in enumerate(read_jsonl(self.chat_file), 1) if i > after]

    def status(self) -> dict:
        st = self.get_state()
        return {"name": self.name, "role": self.meta.get("role", ""),
                "busy": self.proc is not None, "queue": self.queue.qsize(),
                "turns": st["turns"], "max_turns": MAX_TURNS,
                "session": (st["session_id"] or "")[:8], "jobs": sorted(self.running_jobs)}


agents: dict[str, Agent] = {}
registry_lock = threading.Lock()
inbox_lock = threading.Lock()
chat_slots = threading.Semaphore(MAX_PARALLEL_CHATS)
job_slots = threading.Semaphore(MAX_PARALLEL_JOBS)
_seq = 0


def get_agent(name: str) -> Agent | None:
    return agents.get(name)


def validate_name(name: str) -> str | None:
    """Returns an error message, or None when the name can be used for a new agent."""
    if not NAME_RE.match(name):
        return "Name: 2-31 chars, lowercase letters, digits and '-', starting with a letter."
    if name in agents or (AGENTS_DIR / name).exists():
        return f"Agent '{name}' already exists."
    return None


def create_agent(name: str, role: str, start: bool = True) -> Agent:
    """Create agents/<name>/ from the main agent's template files (memory template, skills, settings)."""
    name, role = name.strip(), role.strip()
    with registry_lock:
        if err := validate_name(name):
            raise ValueError(err)
        if not role:
            raise ValueError("Role description is required.")
        path = AGENTS_DIR / name
        path.mkdir(parents=True)
        shutil.copytree(MAIN_DIR / "memory.template", path / "memory")
        for rel in (".claude/settings.json", ".claude/skills/create-skill", ".claude/skills/schedule-job", ".mcp.json"):
            src = MAIN_DIR / rel
            if src.is_dir():
                shutil.copytree(src, path / rel)
            elif src.exists():
                (path / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, path / rel)
        # A copy instead of an @import: imports outside the project need interactive approval, which -p cannot give.
        shared = (MAIN_DIR / "CLAUDE.md").read_text(encoding="utf-8").replace("../tools/", "../../tools/")
        (path / "CLAUDE.md").write_text(ROLE_HEADER.format(name=name, role=role) + shared, encoding="utf-8")
        (path / "jobs.json").write_text("[]", encoding="utf-8")
        save_json(path / "agent.json", {"name": name, "role": role,
                                        "created": datetime.now().isoformat(timespec="seconds")})
        agent = agents[name] = Agent(name, path)
    log(f"[agents] created {name}")
    if start:
        start_worker(agent)
    return agent


def load_agents() -> None:
    agents[MAIN] = Agent(MAIN, MAIN_DIR)
    if AGENTS_DIR.exists():
        for path in sorted(AGENTS_DIR.iterdir()):
            if path.is_dir() and NAME_RE.match(path.name) and (path / "agent.json").exists():
                agents[path.name] = Agent(path.name, path)


# --- inbox: every message is written to disk before processing -----------------
def submit(agent_name: str, text: str, channel: str, reply: dict | None = None, voice: bool = False) -> dict:
    global _seq
    agent = agents[agent_name]
    with inbox_lock:
        _seq += 1
        item = {"seq": _seq, "agent": agent_name, "channel": channel, "reply": reply or {},
                "text": text, "voice": voice, "ts": datetime.now().isoformat(timespec="seconds")}
        append_jsonl(INBOX_FILE, item)
    agent.log_chat("user", text, channel)
    agent.queue.put(item)
    return item


def replay_unprocessed() -> None:
    for item in read_jsonl(INBOX_FILE):
        agent = agents.get(item.get("agent", MAIN))
        if agent and item.get("seq", 0) > agent.get_state()["processed_seq"]:
            log(f"[startup] replaying message {item['seq']} for {agent.name}")
            agent.queue.put(item)


def migrate_legacy_state() -> list[dict]:
    """One-time upgrade from the single-agent layout. Returns legacy inbox items that were never processed."""
    global _seq
    legacy = load_json(STATE_DIR / "state.json", {})
    main = agents[MAIN]
    if not (main.state_dir / "session.json").exists():
        main.update_state(session_id=legacy.get("session_id"), turns=legacy.get("turns", 0), processed_seq=0)
        if (STATE_DIR / "jobs_state.json").exists():
            shutil.move(STATE_DIR / "jobs_state.json", main.jobs_state_file)
    items = read_jsonl(INBOX_FILE)
    old = [i for i in items if "seq" not in i]
    pending = []
    if old:
        done = legacy.get("processed_id", 0)
        pending = [i for i in old if i.get("id", 0) > done]
        INBOX_FILE.replace(INBOX_FILE.with_suffix(".legacy.jsonl"))
    _seq = max((i.get("seq", 0) for i in items), default=0)
    return pending


# --- Claude Code ------------------------------------------------------------------
def run_claude(agent: Agent, prompt: str, session_id: str | None = None, track: bool = True) -> dict:
    """Run one `claude -p` call in the agent's directory. Returns {text, session_id, is_error, cost}."""
    cmd = [CLAUDE_BIN, "-p", "--output-format", "json", "--permission-mode", PERMISSION_MODE]
    model = agent.meta.get("model") or MODEL
    if model:
        cmd += ["--model", model]
    mcp_file = agent.path / ".mcp.json"
    if mcp_file.exists():  # explicit flag: project .mcp.json needs interactive approval, which -p cannot give
        cmd += ["--mcp-config", str(mcp_file)]
    if session_id:
        cmd += ["--resume", session_id]

    proc = subprocess.Popen(
        cmd, cwd=agent.path, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", env={**os.environ, "PROTOBOT_AGENT": agent.name},
    )
    if track:
        agent.proc, agent.stopped = proc, False
    try:
        out, err = proc.communicate(prompt, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return {"text": f"Timed out after {TIMEOUT}s.", "session_id": session_id, "is_error": True, "cost": 0}
    finally:
        if track:
            agent.proc = None

    if track and agent.stopped:
        return {"text": "Stopped.", "session_id": session_id, "is_error": True, "cost": 0}
    try:
        data = json.loads(out)
    except ValueError:
        return {"text": f"Claude Code error: {(err or out)[:500]}", "session_id": session_id,
                "is_error": True, "cost": 0}
    return {
        "text": data.get("result") or "",
        "session_id": data.get("session_id") or session_id,
        "is_error": bool(data.get("is_error")),
        "cost": data.get("total_cost_usd", 0),
    }


def stop(agent: Agent) -> bool:
    proc = agent.proc
    if proc and proc.poll() is None:
        agent.stopped = True
        proc.kill()
        return True
    return False


def _reply(agent: Agent, item: dict, text: str, role: str = "agent") -> None:
    shown = channels[item["channel"]].deliver(agent, item, text)
    agent.log_chat(role, shown, item["channel"])


def ask(agent: Agent, item: dict) -> None:
    channel = channels[item["channel"]]
    st = agent.get_state()
    prompt = f"[{datetime.now():%Y-%m-%d %H:%M} | {channel.label}]\n{item['text']}" + channel.prompt_suffix(item)
    with channel.busy(item), chat_slots:
        res = run_claude(agent, prompt, st["session_id"])
        if res["is_error"] and st["session_id"] and not agent.stopped and "Timed out" not in res["text"]:
            log(f"[claude:{agent.name}] resume failed, retrying with fresh session")
            res = run_claude(
                agent,
                prompt + "\n\n(The previous session is unavailable - read memory/MEMORY.md and memory/handoff.md.)",
            )
    agent.update_state(session_id=res["session_id"], turns=st["turns"] + 1)
    log(f"[claude:{agent.name}] done cost=${res['cost']:.4f} error={res['is_error']}")
    _reply(agent, item, res["text"])
    if st["turns"] + 1 >= MAX_TURNS:
        rotate_session(agent, item, "turn limit")


def rotate_session(agent: Agent, item: dict, reason: str) -> None:
    st = agent.get_state()
    if st["session_id"]:
        with channels[item["channel"]].busy(item), chat_slots:
            run_claude(agent, CHECKPOINT_PROMPT, st["session_id"])
    agent.update_state(session_id=None, turns=0)
    _reply(agent, item, f"Session refreshed ({reason}). State saved to memory.", role="system")


def process(agent: Agent, item: dict) -> None:
    try:
        if item["text"].strip().lower() == "/new":
            rotate_session(agent, item, "on request")
        else:
            ask(agent, item)
    except Exception as e:  # keep the worker alive no matter what
        log(f"[worker:{agent.name}] error: {e!r}")
        _reply(agent, item, f"Dispatcher error: {e}", role="system")
    finally:
        agent.update_state(processed_seq=item["seq"])


def start_worker(agent: Agent) -> None:
    def loop():
        while True:
            process(agent, agent.queue.get())

    threading.Thread(target=loop, daemon=True, name=f"worker-{agent.name}").start()


# --- scheduler: jobs.json is edited by each agent, run state lives in state/ -------
def is_due(job: dict, last: datetime | None, now: datetime) -> bool | None:
    """True = run now, False = not yet, None = initialise last_run without running."""
    sched = job.get("schedule", {})
    kind = sched.get("type")
    try:
        if kind == "once":
            return last is None and now >= datetime.fromisoformat(sched["at"])
        if kind == "daily":
            if sched.get("days") and now.weekday() not in sched["days"]:
                return False
            target = datetime.combine(now.date(), datetime.strptime(sched["time"], "%H:%M").time())
            return now >= target and (last is None or last < target) and now - target < timedelta(hours=6)
        if kind == "every":
            if last is None:
                return None
            return now - last >= timedelta(minutes=int(sched["minutes"]))
    except (KeyError, ValueError) as e:
        log(f"[scheduler] bad schedule in job {job.get('id')}: {e}")
    return False


def report_job(agent: Agent, job: dict, text: str) -> None:
    """Main reports in full to Telegram (as before); other agents keep the report in the web UI and notify."""
    jid = job["id"]
    agent.log_chat("job", f"[{jid}] {text}", "job")
    if agent.name == MAIN:
        msg = f"[{jid}] {text}"
        chat_id = notify(msg, job.get("chat_id"))
        if chat_id:
            # The main agent reads this to know what its jobs asked the user (it only sees its own inbox otherwise).
            append_jsonl(STATE_DIR / "sent_questions.jsonl", {"ts": datetime.now().isoformat(timespec="seconds"),
                                                              "job_id": jid, "chat_id": chat_id, "text": msg})
        return
    preview = text if len(text) <= NOTIFY_PREVIEW_CHARS else text[:NOTIFY_PREVIEW_CHARS].rstrip() + "..."
    notify(f"[{agent.name}/{jid}] {preview}\n(full report in the web UI)")


def run_job(agent: Agent, job: dict) -> None:
    jid = job["id"]
    try:
        with job_slots:
            log(f"[job:{agent.name}] start {jid}")
            res = run_claude(
                agent, JOB_HEADER.format(jid=jid, now=f"{datetime.now():%Y-%m-%d %H:%M}", prompt=job["prompt"]),
                None, track=False,
            )
        text = res["text"].strip()
        if text and text != "NO_REPORT":
            report_job(agent, job, text)
        log(f"[job:{agent.name}] done {jid} cost=${res['cost']:.4f}")
    except Exception as e:
        log(f"[job:{agent.name}] {jid} failed: {e!r}")
    finally:
        agent.running_jobs.discard(jid)


def schedule_tick(agent: Agent, now: datetime) -> None:
    jobs = load_json(agent.jobs_file, [])
    js = load_json(agent.jobs_state_file, {})
    for job in jobs if isinstance(jobs, list) else []:
        jid = job.get("id")
        if not jid or not job.get("enabled", True) or jid in agent.running_jobs:
            continue
        raw = js.get(jid, {}).get("last_run")
        due = is_due(job, datetime.fromisoformat(raw) if raw else None, now)
        if due is None or due:
            js[jid] = {"last_run": now.isoformat(timespec="seconds")}
        if due:
            agent.running_jobs.add(jid)
            threading.Thread(target=run_job, args=(agent, job), daemon=True).start()
    save_json(agent.jobs_state_file, js)


def scheduler() -> None:
    while True:
        for agent in list(agents.values()):
            try:
                schedule_tick(agent, datetime.now())
            except Exception as e:
                log(f"[scheduler:{agent.name}] error: {e!r}")
        time.sleep(30)


# --- startup ---------------------------------------------------------------------
def init() -> list[dict]:
    """Prepare directories and state. Returns legacy inbox items to resubmit (see migrate_legacy_state)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Memory and jobs are runtime data (gitignored). On first run create them from the tracked template.
    if not (MAIN_DIR / "memory").exists():
        shutil.copytree(MAIN_DIR / "memory.template", MAIN_DIR / "memory")
    if not (MAIN_DIR / "jobs.json").exists():
        (MAIN_DIR / "jobs.json").write_text("[]", encoding="utf-8")
    load_agents()
    pending = migrate_legacy_state()
    replay_unprocessed()
    return pending


def start() -> None:
    for agent in agents.values():
        start_worker(agent)
    threading.Thread(target=scheduler, daemon=True, name="scheduler").start()
