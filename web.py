"""
Web UI channel: JSON API + one static page. Sees all agents and is the only place to create new ones.

Security: the agent has full permissions on this machine, so the server binds to 127.0.0.1 by default
(reach it through an SSH tunnel or Tailscale) and every /api call needs the WEB_TOKEN.
"""

import os
import secrets
import threading
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


@app.get("/api/agents/{name}/jobs")
def jobs(name: str):
    agent = agent_or_404(name)
    js = core.load_json(agent.jobs_state_file, {})
    return [dict(j, last_run=js.get(j.get("id"), {}).get("last_run")) for j in core.load_json(agent.jobs_file, [])]


def start() -> None:
    if not WEB_TOKEN:
        raise SystemExit("WEB_ENABLED=on needs WEB_TOKEN in .env (any long random string).")
    server = uvicorn.Server(uvicorn.Config(app, host=WEB_HOST, port=WEB_PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True, name="web").start()
    core.log(f"[web] UI on http://{WEB_HOST}:{WEB_PORT}")
