# ProtoBot

A general-purpose personal agent you talk to over Telegram. Engine: Claude Code (`claude -p`).
No database: memory is plain files. `dispatch.py` is only plumbing (Telegram <-> Claude Code,
scheduler, crash safety). Thinking, memory and skills are handled by Claude Code itself inside `agent/`.

```
Telegram <-> dispatch.py --claude -p (--resume)--> agent/ (CLAUDE.md, memory/, skills, jobs.json, Playwright MCP)
                 |-- state/ (offset, session, inbox.jsonl)     scheduler every 30 s -> jobs.json -> fresh session -> Telegram
```

## Quick start (local)

1. Requirements: Python 3.12, Node (for `npx`), Claude Code installed and logged in (`claude` works in a terminal), Chrome.
2. Create your config: copy `.env.example` to `.env` and fill in:
   - `TELEGRAM_BOT_TOKEN`: create a bot with @BotFather (`/newbot`). One token can be polled by only one process at a time.
   - `ALLOWED_USER_IDS`: your numeric Telegram user id (ask @userinfobot). The bot refuses to start without it, because the agent has full permissions.
3. Run `start.bat` (creates a venv, installs dependencies, restarts the dispatcher after a crash).
4. Message the bot. A good first task: "Let's get acquainted: ask me about my goals and preferences and save them to memory."

## Telegram commands
`/new` (save state and start a fresh session), `/stop`, `/status`, `/jobs`, `/help`.
Everything else goes to the agent (text, photos, files, voice notes).

## Voice notes (ElevenLabs, optional)
Set `ELEVENLABS_API_KEY` in `.env`. Without a key voice is disabled and a voice note reaches the agent as a file.
- You send a voice note: speech-to-text (Scribe) transcribes it and the agent answers in three parts: (1) a text "Understood: ..." (2) a short voice note (3) the full answer as text.
- Text messages always get text-only answers. `VOICE_REPLIES=off` disables voice entirely.
- Voice: `ELEVENLABS_VOICE_ID` (default: Sarah). The Free plan only allows "premade" voices (e.g. Sarah, Roger, Laura, George, Charlie, Brian, Lily); library voices fail with 402 `paid_plan_required`.
- Model: `eleven_flash_v2_5` is the cheapest and supports Polish; higher quality: `eleven_multilingual_v2`.
- The voice note is sent as opus (Telegram voice bubble); if Telegram rejects it, an mp3 audio file is sent instead. Failures are logged as `[elevenlabs]` or `[telegram]`.
- Usage counts against your ElevenLabs plan quota. Spoken text is truncated to 700 characters.

## Browser
`agent/.mcp.json` gives the agent Playwright MCP (Chrome), so it can open pages and interact with web apps.
The dispatcher passes it explicitly with `--mcp-config`, because project MCP servers need interactive approval that `claude -p` cannot give.

## Continuity
- The Claude Code session is resumed (`--resume`). After `SESSION_MAX_TURNS` messages the agent saves its state to `memory/handoff.md` and a fresh session starts (small context = lower cost).
- Every message is written to `state/inbox.jsonl` before processing; unfinished ones are replayed after a crash.
- Future and recurring tasks: the agent edits `agent/jobs.json` itself, the dispatcher runs them in fresh sessions.

## What is tracked in git
Only the logic. Runtime data is ignored: `agent/memory/`, `agent/jobs.json`, `state/`, `.env`, skills created by the agent, browser artifacts.
On first start the dispatcher creates `agent/memory/` from the tracked `agent/memory.template/`.
Personal preferences and learned rules live in `agent/memory/rules.md`, not in `agent/CLAUDE.md`.

## Moving to a Linux VM
1. Install Python, Node, Claude Code (`npm i -g @anthropic-ai/claude-code`), log in with `claude`, install Chromium (`npx playwright install chromium`).
2. In `agent/.mcp.json` add `"--headless"` to args and change `--browser chrome` to `--browser chromium` (the VM has no display).
3. Run as a regular user, not root (`bypassPermissions` may be refused for root).
4. Use a process supervisor, e.g. systemd:
   ```
   [Service]
   WorkingDirectory=/home/bot/ProtoBot
   ExecStart=/home/bot/ProtoBot/venv/bin/python dispatch.py
   Restart=always
   User=bot
   ```
5. Copy the agent's "brain" from the old machine: `agent/memory`, `agent/.claude/skills`, `agent/jobs.json`. `state/` can be skipped.
6. Remove `claudeMdExcludes` from `agent/.claude/settings.json` (it only matters when the repo sits under a folder with its own CLAUDE.md).

## Security (important with full autonomy)
- The agent runs with `bypassPermissions`: full access to the machine and to logged-in browser sessions. Keep it on a VM with separate accounts and only the secrets it needs.
- Approval before outward actions is enforced by instructions in `agent/CLAUDE.md` (soft). The hard limit is the permissions of the accounts and tokens you give it.
- `ALLOWED_USER_IDS` is enforced; messages from anyone else are ignored.
- `.env` (token, API keys) is in `.gitignore`.
