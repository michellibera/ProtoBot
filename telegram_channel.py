"""
Telegram channel: talks only to the `main` agent (the original single-agent bot) and carries
notifications from all agents. Extra agents are created and used from the web UI.
"""

import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import requests

import core
from core import MAIN, load_json, log, save_json

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_IDS = [x.strip() for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()]
TG_LIMIT = 4000
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ElevenLabs voice (optional): speech-to-text for incoming voice notes, text-to-speech for replies.
EL_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
EL_VOICE = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL").strip()
EL_TTS_MODEL = os.getenv("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5").strip()
EL_STT_MODEL = os.getenv("ELEVENLABS_STT_MODEL", "scribe_v1").strip()
VOICE_REPLIES = os.getenv("VOICE_REPLIES", "voice_in").strip()  # voice_in (voice note in -> voice reply) | off
VOICE_MAX_CHARS = 700

VOICE_SUFFIX = (
    "\n\n[Voice mode] Reply in EXACTLY this format, with no text outside the tags (use the user's language):\n"
    "<request>1-2 sentences: what you understood the user asked for</request>\n"
    "<voice>short answer to be read aloud: 1-3 sentences, plain words, no links, lists, symbols or code</voice>\n"
    "<full>the full answer as text</full>"
)
TAG_RE = re.compile(r"<(request|voice|full)>(.*?)</\1>", re.DOTALL)

HELP = (
    "Every message goes to the agent (Claude Code).\n"
    "/new - save state to memory and start a fresh session\n"
    "/stop - abort the current task\n"
    "/status - bot status\n"
    "/jobs - scheduled jobs\n"
    "/help - this list"
)

state_lock = threading.Lock()


def state_file() -> Path:
    return core.STATE_DIR / "state.json"


def get_state() -> dict:
    with state_lock:
        st = load_json(state_file(), {})
    st.setdefault("offset", 0)
    st.setdefault("chat_id", int(ALLOWED_IDS[0]) if ALLOWED_IDS else None)
    return st


def update_state(**changes) -> None:
    with state_lock:
        st = load_json(state_file(), {})
        st.update(changes)
        save_json(state_file(), st)


# --- Telegram API -----------------------------------------------------------------
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


def notify(text: str, chat_id: int | None = None) -> int | None:
    chat_id = chat_id or get_state()["chat_id"]
    if chat_id:
        send(chat_id, text)
    return chat_id


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
    files_dir = core.MAIN_DIR / "inbox_files"
    files_dir.mkdir(parents=True, exist_ok=True)
    path = files_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{name_hint}"
    path.write_bytes(r.content)
    return path


# --- ElevenLabs voice -------------------------------------------------------------
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


# --- channel (used by core) ---------------------------------------------------------
class TelegramChannel:
    label = "Telegram"

    def prompt_suffix(self, item: dict) -> str:
        return VOICE_SUFFIX if item.get("voice") else ""

    def busy(self, item: dict):
        return typing(item["reply"]["chat_id"])

    def deliver(self, agent, item: dict, text: str) -> str:
        """Send the agent reply. In voice mode: request summary (text), short voice note, full text."""
        chat_id = item["reply"]["chat_id"]
        parts = {k: v.strip() for k, v in TAG_RE.findall(text)}
        if not item.get("voice") or not parts.get("full"):
            send(chat_id, text)
            return text
        if parts.get("request"):
            send(chat_id, f"Understood: {parts['request']}")
        if parts.get("voice") and not send_voice(chat_id, parts["voice"]):
            send(chat_id, f"(voice unavailable) {parts['voice']}")
        send(chat_id, parts["full"])
        return parts["full"]


# --- commands handled immediately (not queued) --------------------------------------
def handle_command(chat_id: int, cmd: str) -> None:
    main = core.agents[MAIN]
    if cmd in ("/start", "/help"):
        send(chat_id, HELP)
    elif cmd == "/stop":
        send(chat_id, "Stopping the current task." if core.stop(main) else "Nothing is running.")
    elif cmd == "/status":
        s = main.status()
        others = len(core.agents) - 1
        send(chat_id, (
            f"Task: {'running' if s['busy'] else 'idle'}\n"
            f"Queue: {s['queue']}\nSession: {s['session'] + '...' if s['session'] else 'none'} "
            f"(turns: {s['turns']}/{s['max_turns']})\n"
            f"Running jobs: {', '.join(s['jobs']) or 'none'}"
            + (f"\nOther agents: {others} (web UI)" if others else "")
        ))
    elif cmd == "/jobs":
        jobs, js = load_json(main.jobs_file, []), load_json(main.jobs_state_file, {})
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


# --- polling --------------------------------------------------------------------------
def build_text(msg: dict) -> tuple[str | None, bool]:
    """Turn a Telegram message into (agent-readable text, voice_mode). Downloads attachments."""
    caption = (msg.get("text") or msg.get("caption") or "").strip()
    path, kind = None, None
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
    update_state(chat_id=chat_id)
    cmd = text.split()[0].lower().split("@")[0]
    if text.startswith("/") and cmd != "/new":
        handle_command(chat_id, cmd)
        return
    core.submit(MAIN, text, "telegram", reply={"chat_id": chat_id}, voice=voice_mode)


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


def register() -> None:
    core.channels["telegram"] = TelegramChannel()
    core.notifier = notify


def resubmit_legacy(items: list[dict]) -> None:
    for item in items:
        log(f"[startup] replaying legacy message {item.get('id')}")
        core.submit(MAIN, item["text"], "telegram", reply={"chat_id": item["chat_id"]}, voice=item.get("voice", False))
