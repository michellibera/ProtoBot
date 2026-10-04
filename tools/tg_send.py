"""
Send a Telegram message or file to the user. Used by the agent (via Bash) for
proactive updates during long tasks.

  python tools/tg_send.py "text"
  python tools/tg_send.py "caption" --file path/to/file
  python tools/tg_send.py "text" --chat 123456
"""
import argparse
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

parser = argparse.ArgumentParser()
parser.add_argument("text", nargs="?", default="")
parser.add_argument("--file")
parser.add_argument("--chat")
args = parser.parse_args()

token = os.getenv("TELEGRAM_BOT_TOKEN", "")
allowed = [x.strip() for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()]
chat = args.chat or (allowed[0] if allowed else "")
if not token or not chat:
    sys.exit("Missing TELEGRAM_BOT_TOKEN / ALLOWED_USER_IDS in .env")
if chat not in allowed:
    sys.exit("Chat is not on the whitelist")

api = f"https://api.telegram.org/bot{token}"
if args.file:
    path = Path(args.file)
    method, field = ("sendPhoto", "photo") if path.suffix.lower() in (".jpg", ".jpeg", ".png") else ("sendDocument", "document")
    with path.open("rb") as f:
        r = requests.post(f"{api}/{method}", data={"chat_id": chat, "caption": args.text[:1000]},
                          files={field: f}, timeout=120)
else:
    r = requests.post(f"{api}/sendMessage", json={"chat_id": chat, "text": args.text[:4000]}, timeout=20)

result = r.json()
print("OK" if result.get("ok") else f"ERROR: {result.get('description')}")
sys.exit(0 if result.get("ok") else 1)
