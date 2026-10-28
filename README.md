# ProtoBot

A personal agent on Claude Code (`claude -p`). Telegram talks to the main agent; an optional web UI
manages any number of agents. No database: memory and state are plain files.

```
Telegram ──> main agent (agent/)          ┐
Web UI   ──> all agents (agent/, agents/*) ├─ core.py: queue + session per agent, scheduler ─> claude -p (cwd = agent dir)
                                           ┘   state/: inbox (crash replay), sessions, chat history
```

## Quick start
1. Requirements: Python 3.12, Node (`npx`), Claude Code installed and logged in, Chrome or Chromium.
2. `cp .env.example .env` and set:
   - `TELEGRAM_BOT_TOKEN` (@BotFather `/newbot`; one token = one polling process),
   - `ALLOWED_USER_IDS` (your id from @userinfobot; required, the agent has full permissions).
3. Run `start.bat` (Windows) or `./start.sh` (Linux). Both install dependencies and restart after a crash.
4. Message the bot, e.g. "Ask me about my goals and preferences and save them to memory."

## Agents
- An agent is a Claude Code working directory: role in `CLAUDE.md`, `memory/`, `jobs.json`, skills.
- `main` = `agent/`, the only agent on Telegram. Extra agents live in `agents/<name>/`, created from the web UI
  (files copied from `agent/`; later edits of `agent/CLAUDE.md` do not reach existing agents).
- Agents work in parallel (`MAX_PARALLEL_CHATS`, `MAX_PARALLEL_JOBS`).
- Jobs: an agent edits its own `jobs.json`; the scheduler runs due jobs in fresh sessions. Reports of `main` go to
  Telegram (also logged to `state/sent_questions.jsonl`); other agents report in the web UI plus a short Telegram note.
- After `SESSION_MAX_TURNS` messages the agent saves state to `memory/handoff.md` and a fresh session starts.

## Web UI
`.env`: `WEB_ENABLED=on`, `WEB_TOKEN=<random, e.g. openssl rand -hex 32>`. Open `http://127.0.0.1:8765`.
It binds to localhost only. Remote access: `ssh -L 8765:127.0.0.1:8765 <vps>`, or Tailscale
(`sudo tailscale serve --bg 8765`, then the `https://<host>.<tailnet>.ts.net` address). Never `tailscale funnel`.

## Telegram
Commands: `/new` (save state, fresh session), `/stop`, `/status`, `/jobs`, `/help`. Everything else (text,
photos, files, voice notes) goes to `main`.
Voice (optional): set `ELEVENLABS_API_KEY`; a voice note gets a text summary, a short voice reply and the full text.
Free plan: premade voices only (`ELEVENLABS_VOICE_ID`). `VOICE_REPLIES=off` disables replies by voice.

## Backups
```bash
python tools/backup.py create            # memory, learned skills, agents/, state/ -> backups/ (keeps 14; --with-env adds .env)
python tools/backup.py list
python tools/backup.py restore latest    # bot stopped; current state saved as a pre-restore backup first
```
Daily: `0 3 * * * cd ~/ProtoBot && venv/bin/python tools/backup.py create` (crontab). Copy `backups/` off the machine.

## Linux VPS
1. Install Python, Node, Claude Code, log in with `claude`; `npx playwright install chromium`
   (`agent/.mcp.json` already uses headless Chromium).
2. Run as a regular user (not root). systemd unit:
   ```ini
   [Service]
   WorkingDirectory=/home/ubuntu/ProtoBot
   ExecStartPre=/home/ubuntu/ProtoBot/venv/bin/pip install -q -r requirements.txt
   ExecStart=/home/ubuntu/ProtoBot/venv/bin/python dispatch.py
   Restart=always
   User=ubuntu
   ```
   `ExecStartPre` matters: without it new dependencies are never installed.
3. Update: back up, stop, `git pull --ff-only`, start, check the log (`journalctl -u protobot -f`).
   Rollback: `git checkout v1-vps` (last single-agent version) and restore its backup.

## Development
`pip install -r requirements-dev.txt`, `pytest`. Code: `core.py` (agents, queues, sessions, scheduler),
`telegram_channel.py`, `web.py` + `web/index.html`, `dispatch.py` (wiring), `tools/` (`tg_send.py`, `backup.py`).
Git tracks logic only; runtime data (`agent/memory/`, `agents/`, `state/`, `backups/`, `.env`, learned skills) is ignored.

## Security
- The agent runs with `bypassPermissions`: full access to the machine and its logged-in browser sessions.
  Use a separate VM and accounts, give it only the secrets it needs.
- Approval before outward actions is an instruction in `agent/CLAUDE.md` (soft); the hard limit is the permissions
  of the accounts and tokens it has. Agents are not isolated from each other's folders.
- Only `ALLOWED_USER_IDS` can talk to the bot; the web API requires `WEB_TOKEN`.
