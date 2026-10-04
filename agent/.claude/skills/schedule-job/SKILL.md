---
name: schedule-job
description: Schedule a task for later or on a recurring basis (reminders, daily checks, reports, monitoring). Use whenever the user asks for something "later", "every day", "tomorrow at", or a long-running task should be split over time.
---

# Scheduling jobs

Jobs live in `jobs.json` (in the agent working directory) as a JSON list. The dispatcher checks it every 30 s,
runs each due job in a NEW session (no chat history), and sends the reply to the user on Telegram.
Run state is kept by the dispatcher in `../state/`, so do not store run info in `jobs.json`.

## Job format
```json
{
  "id": "morning-briefing",
  "enabled": true,
  "schedule": {"type": "daily", "time": "08:00", "days": [0,1,2,3,4]},
  "prompt": "Self-contained instructions. The job session knows nothing about the chat; tell it which memory files to read."
}
```
Schedule types:
- `{"type": "once", "at": "2026-10-01T09:00"}` local time, runs once (afterwards set `"enabled": false` or remove it)
- `{"type": "daily", "time": "HH:MM", "days": [0-6, Monday=0]}` (`days` optional)
- `{"type": "every", "minutes": 60}` first run happens one interval after creation

## Rules
1. Edit the file safely: read it, add/modify the entry, write valid JSON back. Keep ids unique and kebab-case.
2. The prompt must be self-contained and say what to report. Jobs reply with the exact text `NO_REPORT` when there is nothing worth telling the user, to avoid noise.
3. Jobs that need approval for outward actions must prepare a draft and ask the user in the report, not act.
4. Long project: store the plan in `memory/plans/<topic>.md` and let the job read it, do the next step, update status.
5. Confirm to the user what was scheduled and when it will first run. Add a line to `memory/handoff.md`.
6. To cancel: set `"enabled": false` or delete the entry.
