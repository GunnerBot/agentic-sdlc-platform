from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict

DEV_HERMES_API_KEY = "local-dev-hermes-key"
DEV_MULTICA_API_KEY = "local-dev-multica-key"

app = FastAPI(title="Agentic SDLC local dev agent services")

_sessions: dict[str, dict[str, object]] = {}
_tasks: dict[str, dict[str, object]] = {}


class HermesAskRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    provider: str
    channel: str
    sender_id: str
    text: str
    repo: str | None = None


class HermesStartSessionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: str
    provider: str
    external_thread_id: str
    text: str
    repo: str | None = None


class HermesResumeSessionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    text: str
    actor: str


class MulticaTaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    source: str
    external_id: str
    title: str
    repo: str | None = None
    inbound_event_id: str | None = None
    metadata: dict[str, object] = {}


class MulticaTaskUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    metadata: dict[str, object] = {}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/sessions/ask", status_code=status.HTTP_200_OK)
async def ask_hermes(
    body: HermesAskRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    _require_bearer(authorization, DEV_HERMES_API_KEY)
    session_id = f"dev-hermes-session-{uuid4()}"
    message_id = f"dev-hermes-message-{uuid4()}"
    _sessions[session_id] = {
        "provider": body.provider,
        "channel": body.channel,
        "sender_id": body.sender_id,
        "repo": body.repo,
        "messages": [{"actor": body.sender_id, "text": body.text}],
    }
    return {
        "session_id": session_id,
        "message_id": message_id,
        "answer": _answer(body.text, body.repo),
    }


@app.post("/api/sessions", status_code=status.HTTP_201_CREATED)
async def start_hermes_session(
    body: HermesStartSessionRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    _require_bearer(authorization, DEV_HERMES_API_KEY)
    session_id = f"dev-hermes-session-{uuid4()}"
    message_id = f"dev-hermes-message-{uuid4()}"
    _sessions[session_id] = {
        "task_id": body.task_id,
        "provider": body.provider,
        "external_thread_id": body.external_thread_id,
        "repo": body.repo,
        "messages": [{"actor": "system", "text": body.text}],
    }
    return {
        "session_id": session_id,
        "message_id": message_id,
        "answer": _answer(body.text, body.repo),
    }


@app.post("/api/sessions/{session_id}/messages", status_code=status.HTTP_200_OK)
async def resume_hermes_session(
    session_id: str,
    body: HermesResumeSessionRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    _require_bearer(authorization, DEV_HERMES_API_KEY)
    session = _sessions.setdefault(session_id, {"messages": []})
    messages = session.setdefault("messages", [])
    if isinstance(messages, list):
        messages.append({"actor": body.actor, "text": body.text})
    return {
        "session_id": session_id,
        "message_id": f"dev-hermes-message-{uuid4()}",
        "answer": _answer(body.text, _repo_from_session(session)),
    }


@app.post("/api/tasks", status_code=status.HTTP_201_CREATED)
async def create_multica_task(
    body: MulticaTaskCreateRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    _require_bearer(authorization, DEV_MULTICA_API_KEY)
    task_id = f"dev-multica-task-{uuid4()}"
    task = {
        "id": task_id,
        "status": "queued",
        "source": body.source,
        "external_id": body.external_id,
        "title": body.title,
        "repo": body.repo,
        "inbound_event_id": body.inbound_event_id,
        "metadata": body.metadata,
    }
    _tasks[task_id] = task
    return task


@app.patch("/api/tasks/{task_id}", status_code=status.HTTP_200_OK)
async def update_multica_task(
    task_id: str,
    body: MulticaTaskUpdateRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    _require_bearer(authorization, DEV_MULTICA_API_KEY)
    task = _tasks.setdefault(task_id, {"id": task_id, "metadata": {}})
    task["status"] = body.status
    task["metadata"] = body.metadata
    return task


def _require_bearer(authorization: str | None, expected_token: str) -> None:
    if authorization != f"Bearer {expected_token}":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid local dev service token",
        )


def _answer(text: str, repo: str | None) -> str:
    repo_text = f" for {repo}" if repo else ""
    return f"Local Hermes dev response{repo_text}: received {text[:120]}"


def _repo_from_session(session: dict[str, object]) -> str | None:
    repo = session.get("repo")
    return repo if isinstance(repo, str) else None
