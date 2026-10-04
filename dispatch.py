"""
ProtoBot dispatcher: Telegram <-> Claude Code (claude -p).

Only infrastructure lives here (messages in/out, sessions, scheduler, crash safety).
All "thinking", memory and skills live in agent/ and are handled by Claude Code itself.
State is kept in plain files (state/), no database.

Run: python dispatch.py
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

AGENT_DIR = ROOT / "agent"
STATE_DIR = ROOT / "state"
FILES_DIR = AGENT_DIR / "inbox_files"
STATE_FILE = STATE_DIR / "state.json"
INBOX_FILE = STATE_DIR / "inbox.jsonl"
JOBS_FILE = AGENT_DIR / "jobs.json"
JOBS_STATE_FILE = STATE_DIR / "jobs_state.json"
for d in (STATE_DIR, FILES_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Memory and jobs are runtime data (gitignored). On first run create them from the tracked template.
if not (AGENT_DIR / "memory").exists():
    shutil.copytree(AGENT_DIR / "memory.template", AGENT_DIR / "memory")
if not JOBS_FILE.exists():
    JOBS_FILE.write_text("[]", encoding="utf-8")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_IDS = [x.strip() for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()]
CLAUDE_BIN = os.getenv("CLAUDE_BIN") or shutil.which("claude")
MODEL = os.getenv("CLAUDE_MODEL", "").strip()
PERMISSION_MODE = os.getenv("CLAUDE_PERMISSION_MODE", "bypassPermissions")
TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "1800"))
MAX_TURNS = int(os.getenv("SESSION_MAX_TURNS", "40"))
MAX_PARALLEL_JOBS = int(os.getenv("MAX_PARALLEL_JOBS", "2"))
TG_LIMIT = 4000

# ElevenLabs voice (optional): speech-to-text for incoming voice notes, text-to-speech for replies.
EL_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
EL_VOICE = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL").strip()
EL_TTS_MODEL = os.getenv("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5").strip()
EL_STT_MODEL = os.getenv("ELEVENLABS_STT_MODEL", "scribe_v1").strip()
VOICE_REPLIES = os.getenv("VOICE_REPLIES", "voice_in").strip()  # voice_in (voice note in -> voice reply) | off
VOICE_MAX_CHARS = 700

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

CHECKPOINT_PROMPT = (
    "This session is about to be closed (context reset). Save your state to files now: "
    "update memory/handoff.md (current goals, agreements with the user, task statuses, "
    "pending questions, next steps), add durable facts to memory/ and update MEMORY.md. "
    "Reply with one word: OK."
)
VOICE_SUFFIX = (
    "\n\n[Voice mode] Reply in EXACTLY this format, with no text outside the tags (use the user's language):\n"
    "<request>1-2 sentences: what you understood the user asked for</request>\n"
    "<voice>short answer to be read aloud: 1-3 sentences, plain words, no links, lists, symbols or code</voice>\n"
    "<full>the full answer as text</full>"
)
TAG_RE = re.compile(r"<(request|voice|full)>(.*?)</\1>", re.DOTALL)
JOB_HEADER = (
    "[Scheduled run | job: {jid} | {now}]\n"
    "New session with no chat history. Start by reading memory/MEMORY.md and memory/handoff.md. "
    "Do the task below. Finish with a short report for the user. "
    "If there is nothing worth reporting, reply with exactly: NO_REPORT\n\nTASK:\n{prompt}"
)

state_lock = threading.Lock()
inbox_lock = threading.Lock()
work_queue: "queue.Queue[dict]" = queue.Queue()
current = {"proc": None, "stopped": False}
running_jobs: set[str] = set()
job_slots = threading.Semaphore(MAX_PARALLEL_JOBS)


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# --- small file helpers -------------------------------------------------------
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def get_state() -> dict:
    with state_lock:
        st = load_json(STATE_FILE, {})
    st.setdefault("offset", 0)
    st.setdefault("processed_id", 0)
    st.setdefault("session_id", None)
    st.setdefault("turns", 0)
    st.setdefault("chat_id", int(ALLOWED_IDS[0]) if ALLOWED_IDS else None)
    return st


def update_state(**changes) -> None:
    with state_lock:
        st = load_json(STATE_FILE, {})
        st.update(changes)
        save_json(STATE_FILE, st)


# --- Telegram -----------------------------------------------------------------
def tg(method: str, timeout: int = 15, **params) -> dict:
    try:
        r = requests.post(f"{API}/{method}", json=params, timeout=timeout)
        return r.json()
    except (requests.RequestException, ValueError) as e:
        log(f"[telegram] {method} failed: {e}")
        return {"ok": False}


def split_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


def send(chat_id: int, text: str) -> None:
    # Plain text on purpose: no parse_mode means no formatting errors.
    for chunk in split_text(text.strip() or "(empty reply)"):
        tg("sendMessage", chat_id=chat_id, text=chunk)


@contextmanager
def typing(chat_id: int):
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            tg("sendChatAction", chat_id=chat_id, action="typing")
            stop.wait(4)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()


def download_file(file_id: str, name_hint: str) -> Path | None:
    info = tg("getFile", file_id=file_id)
    if not info.get("ok"):
        return None
    try:
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['result']['file_path']}"
        r = requests.get(url, timeout=60)
        r.raise_for_status()
    except (requests.RequestException, KeyError) as e:
        log(f"[telegram] download failed: {e}")
        return None
    path = FILES_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{name_hint}"
    path.write_bytes(r.content)
    return path


# --- ElevenLabs voice ---------------------------------------------------------
def transcribe(path: Path) -> str | None:
    try:
        with path.open("rb") as f:
            r = requests.post(
                "https://api.elevenlabs.io/v1/speech-to-text",
                headers={"xi-api-key": EL_KEY}, data={"model_id": EL_STT_MODEL},
                files={"file": (path.name, f)}, timeout=120,
            )
        r.raise_for_status()
        return (r.json().get("text") or "").strip() or None
    except (requests.RequestException, ValueError, OSError) as e:
        log(f"[elevenlabs] transcription failed: {e}")
        return None


def synthesize(text: str, output_format: str) -> bytes | None:
    try:
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{EL_VOICE}",
            headers={"xi-api-key": EL_KEY}, params={"output_format": output_format},
            json={"text": text, "model_id": EL_TTS_MODEL}, timeout=120,
        )
        if not r.ok:
            log(f"[elevenlabs] tts {r.status_code} ({output_format}): {r.text[:300]}")
            return None
        return r.content
    except requests.RequestException as e:
        log(f"[elevenlabs] tts failed ({output_format}): {e}")
        return None


def send_voice(chat_id: int, text: str) -> bool:
    """Speak `text` as a Telegram voice note. Falls back to an mp3 audio file. True on success."""
    text = text[:VOICE_MAX_CHARS]
    attempts = [("opus_48000_64", "sendVoice", "voice", "reply.ogg", "audio/ogg"),
                ("mp3_44100_64", "sendAudio", "audio", "reply.mp3", "audio/mpeg")]
    for fmt, method, field, fname, mime in attempts:
        audio = synthesize(text, fmt)
        if not audio:
            continue
        try:
            r = requests.post(f"{API}/{method}", data={"chat_id": chat_id},
                              files={field: (fname, audio, mime)}, timeout=60)
            if r.json().get("ok"):
                return True
            log(f"[telegram] {method} rejected: {r.text[:200]}")
        except (requests.RequestException, ValueError) as e:
            log(f"[telegram] {method} failed: {e}")
    return False


def deliver(chat_id: int, text: str, voice_mode: bool) -> None:
    """Send the agent reply. In voice mode: request summary (text), short voice note, full text."""
    parts = {k: v.strip() for k, v in TAG_RE.findall(text)}
    if not voice_mode or not parts.get("full"):
        send(chat_id, text)
        return
    if parts.get("request"):
        send(chat_id, f"Understood: {parts['request']}")
    if parts.get("voice") and not send_voice(chat_id, parts["voice"]):
        send(chat_id, f"(voice unavailable) {parts['voice']}")
    send(chat_id, parts["full"])


# --- Claude Code --------------------------------------------------------------
def run_claude(prompt: str, session_id: str | None = None, chat: bool = True) -> dict:
    """Run one `claude -p` call. Returns {text, session_id, is_error, cost}."""
    cmd = [CLAUDE_BIN, "-p", "--output-format", "json", "--permission-mode", PERMISSION_MODE]
    if MODEL:
        cmd += ["--model", MODEL]
    mcp_file = AGENT_DIR / ".mcp.json"
    if mcp_file.exists():  # explicit flag: project .mcp.json needs interactive approval, which -p cannot give
        cmd += ["--mcp-config", str(mcp_file)]
    if session_id:
        cmd += ["--resume", session_id]

    proc = subprocess.Popen(
        cmd, cwd=AGENT_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
    )
    if chat:
        current["proc"], current["stopped"] = proc, False
    try:
        out, err = proc.communicate(prompt, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return {"text": f"Timed out after {TIMEOUT}s.", "session_id": session_id, "is_error": True, "cost": 0}
    finally:
        if chat:
            current["proc"] = None

    if chat and current["stopped"]:
        return {"text": "Stopped (/stop).", "session_id": session_id, "is_error": True, "cost": 0}
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


def ask(chat_id: int, text: str, voice_mode: bool = False) -> None:
    st = get_state()
    prompt = f"[{datetime.now():%Y-%m-%d %H:%M} | Telegram]\n{text}" + (VOICE_SUFFIX if voice_mode else "")
    with typing(chat_id):
        res = run_claude(prompt, st["session_id"])
        if res["is_error"] and st["session_id"] and not current["stopped"] and "Timed out" not in res["text"]:
            log("[claude] resume failed, retrying with fresh session")
            res = run_claude(
                prompt + "\n\n(The previous session is unavailable - read memory/MEMORY.md and memory/handoff.md.)",
                None,
            )
    update_state(session_id=res["session_id"], turns=st["turns"] + 1, chat_id=chat_id)
    log(f"[claude] done cost=${res['cost']:.4f} error={res['is_error']}")
    deliver(chat_id, res["text"], voice_mode)
    if st["turns"] + 1 >= MAX_TURNS:
        rotate_session(chat_id, "limit tur")


def rotate_session(chat_id: int, reason: str) -> None:
    st = get_state()
    if st["session_id"]:
        with typing(chat_id):
            run_claude(CHECKPOINT_PROMPT, st["session_id"])
    update_state(session_id=None, turns=0)
    send(chat_id, f"Session refreshed ({reason}). State saved to memory.")


# --- worker: processes chat messages one at a time ------------------------------
def process(item: dict) -> None:
    chat_id, text = item["chat_id"], item["text"]
    if text.strip().lower() == "/new":
        rotate_session(chat_id, "on request")
    else:
        ask(chat_id, text, item.get("voice", False))


def worker() -> None:
    while True:
        item = work_queue.get()
        try:
            process(item)
        except Exception as e:  # keep the worker alive no matter what
            log(f"[worker] error: {e!r}")
            send(item["chat_id"], f"Dispatcher error: {e}")
        finally:
            update_state(processed_id=item["id"])


# --- scheduler: jobs.json is edited by the agent, run state lives in state/ -----
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


def run_job(job: dict) -> None:
    jid = job["id"]
    chat_id = job.get("chat_id") or get_state()["chat_id"]
    try:
        with job_slots:
            log(f"[job] start {jid}")
            res = run_claude(
                JOB_HEADER.format(jid=jid, now=f"{datetime.now():%Y-%m-%d %H:%M}", prompt=job["prompt"]),
                None, chat=False,
            )
        text = res["text"].strip()
        if text and text != "NO_REPORT" and chat_id:
            send(chat_id, f"[{jid}] {text}")
        log(f"[job] done {jid} cost=${res['cost']:.4f}")
    except Exception as e:
        log(f"[job] {jid} failed: {e!r}")
    finally:
        running_jobs.discard(jid)


def scheduler() -> None:
    while True:
        try:
            jobs = load_json(JOBS_FILE, [])
            js = load_json(JOBS_STATE_FILE, {})
            now = datetime.now()
            for job in jobs if isinstance(jobs, list) else []:
                jid = job.get("id")
                if not jid or not job.get("enabled", True) or jid in running_jobs:
                    continue
                raw = js.get(jid, {}).get("last_run")
                due = is_due(job, datetime.fromisoformat(raw) if raw else None, now)
                if due is None:
                    js[jid] = {"last_run": now.isoformat(timespec="seconds")}
                elif due:
                    js[jid] = {"last_run": now.isoformat(timespec="seconds")}
                    running_jobs.add(jid)
                    threading.Thread(target=run_job, args=(job,), daemon=True).start()
            save_json(JOBS_STATE_FILE, js)
        except Exception as e:
            log(f"[scheduler] error: {e!r}")
        time.sleep(30)


# --- commands handled immediately (not queued) ----------------------------------
HELP = (
    "Every message goes to the agent (Claude Code).\n"
    "/new - save state to memory and start a fresh session\n"
    "/stop - abort the current task\n"
    "/status - bot status\n"
    "/jobs - scheduled jobs\n"
    "/help - this list"
)


def handle_command(chat_id: int, cmd: str) -> None:
    if cmd in ("/start", "/help"):
        send(chat_id, HELP)
    elif cmd == "/stop":
        proc = current["proc"]
        if proc and proc.poll() is None:
            current["stopped"] = True
            proc.kill()
            send(chat_id, "Stopping the current task.")
        else:
            send(chat_id, "Nothing is running.")
    elif cmd == "/status":
        st = get_state()
        sid = st["session_id"][:8] + "..." if st["session_id"] else "none"
        send(chat_id, (
            f"Task: {'running' if current['proc'] else 'idle'}\n"
            f"Queue: {work_queue.qsize()}\nSession: {sid} (turns: {st['turns']}/{MAX_TURNS})\n"
            f"Running jobs: {', '.join(running_jobs) or 'none'}"
        ))
    elif cmd == "/jobs":
        jobs, js = load_json(JOBS_FILE, []), load_json(JOBS_STATE_FILE, {})
        if not jobs:
            send(chat_id, "No scheduled jobs.")
            return
        lines = [
            f"{'on ' if j.get('enabled', True) else 'off'} {j.get('id')}: {j.get('schedule')} "
            f"(last run: {js.get(j.get('id'), {}).get('last_run', '-')})"
            for j in jobs
        ]
        send(chat_id, "\n".join(lines))
    else:
        send(chat_id, "Unknown command. /help")


# --- Telegram polling -----------------------------------------------------------
def build_text(msg: dict) -> tuple[str | None, bool]:
    """Turn a Telegram message into (agent-readable text, voice_mode). Downloads attachments."""
    caption = (msg.get("text") or msg.get("caption") or "").strip()
    path, kind, voice_in = None, None, False
    if msg.get("photo"):
        best = msg["photo"][-1]
        path, kind = download_file(best["file_id"], f"{best['file_unique_id']}.jpg"), "a photo"
    elif msg.get("document"):
        doc = msg["document"]
        path, kind = download_file(doc["file_id"], doc.get("file_name", "file")), "a file"
    elif msg.get("voice") or msg.get("audio"):
        media = msg.get("voice") or msg.get("audio")
        path, kind = download_file(media["file_id"], f"{media['file_unique_id']}.ogg"), "an audio recording"
        if path and EL_KEY and VOICE_REPLIES != "off":
            transcript = transcribe(path)
            if transcript:
                return f"[voice note, transcript] {transcript}", True
    if kind:
        if path is None:
            return None, False
        return f"The user sent {kind}: {path}\n{caption}".strip(), False
    return (caption or None), False


def handle_update(update: dict) -> None:
    msg = update.get("message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    user_id = str(msg.get("from", {}).get("id", ""))
    if user_id not in ALLOWED_IDS:
        log(f"[auth] ignored message from user {user_id}")
        return
    text, voice_mode = build_text(msg)
    if not text:
        return
    cmd = text.split()[0].lower().split("@")[0]
    if text.startswith("/") and cmd != "/new":
        handle_command(chat_id, cmd)
        return
    item = {"id": update["update_id"], "chat_id": chat_id, "text": text,
            "voice": voice_mode, "ts": datetime.now().isoformat(timespec="seconds")}
    with inbox_lock:  # write to disk BEFORE processing so a crash cannot lose it
        with INBOX_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    work_queue.put(item)


def replay_unprocessed() -> None:
    done = get_state()["processed_id"]
    if not INBOX_FILE.exists():
        return
    for line in INBOX_FILE.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item["id"] > done:
            log(f"[startup] replaying unprocessed message {item['id']}")
            work_queue.put(item)


def poll() -> None:
    offset = get_state()["offset"]
    while True:
        try:
            r = requests.get(f"{API}/getUpdates", timeout=45,
                             params={"offset": offset, "timeout": 30, "allowed_updates": '["message"]'})
            data = r.json()
        except (requests.RequestException, ValueError):
            time.sleep(3)
            continue
        if not data.get("ok"):
            log(f"[telegram] getUpdates error: {data.get('description')}")
            time.sleep(5)
            continue
        for upd in data["result"]:
            try:
                handle_update(upd)
            except Exception as e:
                log(f"[update] error: {e!r}")
            offset = upd["update_id"] + 1
            update_state(offset=offset)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not BOT_TOKEN:
        sys.exit("Missing TELEGRAM_BOT_TOKEN in .env (see README, step 2).")
    if not ALLOWED_IDS:
        sys.exit("Missing ALLOWED_USER_IDS in .env. The agent has full permissions, so the whitelist is required.")
    if not CLAUDE_BIN:
        sys.exit("Command 'claude' not found. Install Claude Code and log in (run: claude).")
    log(f"ProtoBot start | claude={CLAUDE_BIN} | cwd={AGENT_DIR} | users={ALLOWED_IDS}")
    replay_unprocessed()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    poll()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Shutting down...")
