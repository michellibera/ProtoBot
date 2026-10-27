"""
ProtoBot dispatcher: wires the core (agents, sessions, scheduler) to the channels.

- Telegram: always on, talks to the `main` agent, carries notifications from all agents.
- Web UI (WEB_ENABLED=on): all agents, the only place to create new ones.

Run: python dispatch.py
"""

import os
import sys

import core
import telegram_channel as telegram

WEB_ENABLED = os.getenv("WEB_ENABLED", "off").strip().lower() in ("on", "1", "true", "yes")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not telegram.BOT_TOKEN:
        sys.exit("Missing TELEGRAM_BOT_TOKEN in .env (see README, step 2).")
    if not telegram.ALLOWED_IDS:
        sys.exit("Missing ALLOWED_USER_IDS in .env. The agent has full permissions, so the whitelist is required.")
    if not core.CLAUDE_BIN:
        sys.exit("Command 'claude' not found. Install Claude Code and log in (run: claude).")

    telegram.register()
    legacy = core.init()
    core.log(f"ProtoBot start | claude={core.CLAUDE_BIN} | agents={list(core.agents)} | users={telegram.ALLOWED_IDS}")
    core.start()
    telegram.resubmit_legacy(legacy)
    if WEB_ENABLED:
        import web  # only needs fastapi/uvicorn when the web UI is on
        web.start()
    telegram.poll()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        core.log("Shutting down...")
