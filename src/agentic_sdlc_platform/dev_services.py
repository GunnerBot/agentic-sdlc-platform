from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict

DEV_HERMES_API_KEY = "local-dev-hermes-key"
DEV_MULTICA_API_KEY = "local-dev-multica-key"

app = FastAPI(title="Agentic SDLC local dev agent services")

_sessions: dict[str, dict[str, object]] = {}
_tasks: dict[str, dict[str, object]] = {}
_issues: dict[str, dict[str, object]] = {}
_agents: dict[str, dict[str, object]] = {}
_comments: dict[str, list[dict[str, object]]] = {}
_runtimes: list[dict[str, object]] = [
    {
        "id": "dev-runtime-codex",
        "provider": "codex",
        "status": "online",
    },
    {
        "id": "dev-runtime-hermes",
        "provider": "hermes",
        "status": "online",
    },
]


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


class MulticaIssueCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str
    description: str
    status: str = "todo"
    priority: str = "none"
    assignee_type: str | None = None
    assignee_id: str | None = None


class MulticaAgentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    description: str | None = None
    instructions: str | None = None
    runtime_id: str
    visibility: str = "workspace"
    max_concurrent_tasks: int = 1


class MulticaIssueCommentRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str = "comment"
    content: str


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


@app.get("/api/runtimes", status_code=status.HTTP_200_OK)
async def list_multica_runtimes(
    authorization: str | None = Header(default=None),
) -> list[dict[str, object]]:
    _require_bearer(authorization, DEV_MULTICA_API_KEY)
    return _runtimes


@app.get("/api/agents", status_code=status.HTTP_200_OK)
async def list_multica_agents(
    authorization: str | None = Header(default=None),
) -> list[dict[str, object]]:
    _require_bearer(authorization, DEV_MULTICA_API_KEY)
    return list(_agents.values())


@app.post("/api/agents", status_code=status.HTTP_201_CREATED)
async def create_multica_agent(
    body: MulticaAgentCreateRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    _require_bearer(authorization, DEV_MULTICA_API_KEY)
    agent_id = f"dev-multica-agent-{uuid4()}"
    agent = {
        "id": agent_id,
        "name": body.name,
        "description": body.description,
        "instructions": body.instructions,
        "runtime_id": body.runtime_id,
        "visibility": body.visibility,
        "max_concurrent_tasks": body.max_concurrent_tasks,
        "archived_at": None,
    }
    _agents[agent_id] = agent
    return agent


@app.get("/api/agents/{agent_id}/tasks", status_code=status.HTTP_200_OK)
async def list_multica_agent_tasks(
    agent_id: str,
    authorization: str | None = Header(default=None),
) -> list[dict[str, object]]:
    _require_bearer(authorization, DEV_MULTICA_API_KEY)
    return [task for task in _tasks.values() if task.get("agent_id") == agent_id]


@app.post("/api/issues", status_code=status.HTTP_201_CREATED)
async def create_multica_issue(
    body: MulticaIssueCreateRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    _require_bearer(authorization, DEV_MULTICA_API_KEY)
    issue_id = f"dev-multica-issue-{uuid4()}"
    issue = {
        "id": issue_id,
        "key": issue_id,
        "title": body.title,
        "description": body.description,
        "status": body.status,
        "priority": body.priority,
        "assignee_type": body.assignee_type,
        "assignee_id": body.assignee_id,
    }
    _issues[issue_id] = issue
    _comments[issue_id] = []
    if body.assignee_type == "agent" and body.assignee_id:
        task_id = f"dev-multica-task-{uuid4()}"
        agent = _agents.get(body.assignee_id, {})
        task = {
            "id": task_id,
            "issue_id": issue_id,
            "agent_id": body.assignee_id,
            "runtime_id": agent.get("runtime_id"),
            "status": "queued",
            "result": {
                "output": "Local Multica dev task queued; no real code execution ran.",
            },
        }
        _tasks[task_id] = task
    return issue


@app.get("/api/issues/{issue_id}/task-runs", status_code=status.HTTP_200_OK)
async def list_multica_issue_task_runs(
    issue_id: str,
    authorization: str | None = Header(default=None),
) -> list[dict[str, object]]:
    _require_bearer(authorization, DEV_MULTICA_API_KEY)
    return [task for task in _tasks.values() if task.get("issue_id") == issue_id]


@app.get("/api/issues/{issue_id}/comments", status_code=status.HTTP_200_OK)
async def list_multica_issue_comments(
    issue_id: str,
    authorization: str | None = Header(default=None),
) -> list[dict[str, object]]:
    _require_bearer(authorization, DEV_MULTICA_API_KEY)
    return _comments.setdefault(issue_id, [])


@app.post("/api/issues/{issue_id}/comments", status_code=status.HTTP_201_CREATED)
async def create_multica_issue_comment(
    issue_id: str,
    body: MulticaIssueCommentRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    _require_bearer(authorization, DEV_MULTICA_API_KEY)
    comment = {
        "id": f"dev-multica-comment-{uuid4()}",
        "type": body.type,
        "content": body.content,
        "actor": "local-dev",
    }
    _comments.setdefault(issue_id, []).append(comment)
    return comment


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
