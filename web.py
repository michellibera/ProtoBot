"""
Web UI channel: JSON API + one static page. Sees all agents and is the only place to create new ones.

Security: the agent has full permissions on this machine, so the server binds to 127.0.0.1 by default
(reach it through an SSH tunnel or Tailscale) and every /api call needs the WEB_TOKEN.
"""

import os
import secrets
import threading
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import core

WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1").strip()
WEB_PORT = int(os.getenv("WEB_PORT", "8765"))
WEB_TOKEN = os.getenv("WEB_TOKEN", "").strip()
INDEX = Path(__file__).resolve().parent / "web" / "index.html"

app = FastAPI(title="ProtoBot", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def require_token(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        given = request.headers.get("x-token", "")
        if not WEB_TOKEN or not secrets.compare_digest(given, WEB_TOKEN):
            return JSONResponse({"detail": "Invalid token"}, status_code=401)
    return await call_next(request)


class NewAgent(BaseModel):
    name: str
    role: str


class Message(BaseModel):
    text: str


class Settings(BaseModel):
    role: str | None = None
    model: str | None = None
    max_turns: int | None = None
    paused: bool | None = None


class Rule(BaseModel):
    text: str


class Job(BaseModel):
    id: str
    enabled: bool = True
    schedule: dict
    prompt: str


def agent_or_404(name: str) -> core.Agent:
    agent = core.get_agent(name)
    if not agent:
        raise HTTPException(404, f"No agent '{name}'")
    return agent


@app.get("/")
def index():
    return FileResponse(INDEX)


@app.get("/api/agents")
def list_agents():
    return [a.status() for a in list(core.agents.values())]


@app.post("/api/agents", status_code=201)
def create_agent(body: NewAgent):
    try:
        return core.create_agent(body.name, body.role).status()
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/agents/{name}")
def delete_agent(name: str):
    agent_or_404(name)
    try:
        core.delete_agent(name)
    except ValueError as e:
        raise HTTPException(409, str(e))
    except KeyError:  # deleted concurrently
        raise HTTPException(404, f"No agent '{name}'")
    return {"deleted": name}


@app.get("/api/agents/{name}/messages")
def messages(name: str, after: int = 0):
    return agent_or_404(name).read_chat(after)


@app.post("/api/agents/{name}/messages", status_code=202)
def post_message(name: str, body: Message):
    agent_or_404(name)
    if not body.text.strip():
        raise HTTPException(400, "Empty message")
    return {"seq": core.submit(name, body.text.strip(), "web")["seq"]}


@app.post("/api/agents/{name}/new", status_code=202)
def new_session(name: str):
    agent_or_404(name)
    return {"seq": core.submit(name, "/new", "web")["seq"]}


@app.post("/api/agents/{name}/stop")
def stop(name: str):
    return {"stopped": core.stop(agent_or_404(name))}


@app.patch("/api/agents/{name}")
def update_agent(name: str, body: Settings):
    agent = agent_or_404(name)
    try:
        core.update_settings(agent, **body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return agent.status()


@app.get("/api/agents/{name}/setup")
def setup(name: str):
    agent = agent_or_404(name)
    files = core.memory_files(agent)
    return {"role": agent.meta.get("role", ""), "role_editable": name != core.MAIN,
            "rules": core.read_rules(agent),
            "knowledge": [f for f in files if f["path"].startswith("knowledge/")],
            "memory": [f for f in files if not f["path"].startswith("knowledge/")],
            "model": agent.meta.get("model", ""), "default_model": core.MODEL,
            "max_turns": agent.meta.get("max_turns", 0), "default_max_turns": core.MAX_TURNS}


@app.post("/api/agents/{name}/rules", status_code=201)
def add_rule(name: str, body: Rule):
    agent = agent_or_404(name)
    try:
        core.add_rule(agent, body.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return core.read_rules(agent)


@app.post("/api/agents/{name}/rules/delete")
def delete_rule(name: str, body: Rule):
    agent = agent_or_404(name)
    try:
        core.delete_rule(agent, body.text)
    except KeyError:
        raise HTTPException(404, "No such rule (changed meanwhile?)")
    return core.read_rules(agent)


@app.get("/api/agents/{name}/memory")
def memory_file(name: str, path: str):
    try:
        return {"path": path, "text": core.read_memory_file(agent_or_404(name), path)}
    except KeyError:
        raise HTTPException(404, f"No file '{path}'")


@app.get("/api/agents/{name}/jobs")
def jobs(name: str):
    agent = agent_or_404(name)
    js = core.load_json(agent.jobs_state_file, {})
    now = datetime.now()
    out = []
    for j in core.load_jobs(agent):
        last = js.get(j.get("id"), {}).get("last_run")
        nxt = None if agent.paused else core.next_run(j, datetime.fromisoformat(last) if last else None, now)
        out.append(dict(j, last_run=last, next_run=nxt and nxt.isoformat(timespec="seconds"),
                        running=j.get("id") in agent.running_jobs))
    return out


@app.post("/api/agents/{name}/jobs", status_code=201)
def create_job(name: str, body: Job):
    try:
        return core.save_job(agent_or_404(name), body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/agents/{name}/jobs/{jid}")
def update_job(name: str, jid: str, body: Job):
    try:
        return core.save_job(agent_or_404(name), body.model_dump(), old_id=jid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(404, f"No job '{jid}'")


@app.delete("/api/agents/{name}/jobs/{jid}")
def delete_job(name: str, jid: str):
    try:
        core.delete_job(agent_or_404(name), jid)
    except KeyError:
        raise HTTPException(404, f"No job '{jid}'")
    return {"deleted": jid}


@app.post("/api/agents/{name}/jobs/{jid}/run", status_code=202)
def run_job(name: str, jid: str):
    try:
        core.run_job_now(agent_or_404(name), jid)
    except KeyError:
        raise HTTPException(404, f"No job '{jid}'")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"started": jid}


def start() -> None:
    if not WEB_TOKEN:
        raise SystemExit("WEB_ENABLED=on needs WEB_TOKEN in .env (any long random string).")
    server = uvicorn.Server(uvicorn.Config(app, host=WEB_HOST, port=WEB_PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True, name="web").start()
    core.log(f"[web] UI on http://{WEB_HOST}:{WEB_PORT}")
