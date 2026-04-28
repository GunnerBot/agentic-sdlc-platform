import json

import httpx
import pytest

from agentic_sdlc_platform.adapters.multica import MulticaTaskOrchestrator
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.task_orchestrator import (
    TaskCommentRequest,
    TaskOrchestratorError,
    TaskReadRequest,
    TaskRequest,
    TaskUpdateRequest,
)


def multica_settings(**overrides) -> Settings:
    defaults = {
        "multica_http_enabled": True,
        "multica_base_url": "https://multica.local",
        "multica_api_key": "test-key",
        "multica_workspace_id": "workspace-1",
        "multica_default_runtime_provider": "codex",
    }
    defaults.update(overrides)
    return Settings(**defaults)


async def test_multica_adapter_blocks_when_http_disabled() -> None:
    orchestrator = MulticaTaskOrchestrator(Settings(multica_http_enabled=False))

    with pytest.raises(TaskOrchestratorError, match="multica HTTP is disabled"):
        await orchestrator.create_task(
            TaskRequest(
                source="linear",
                external_id="OS-1284",
                title="Build webhook bridge",
                repo="keychain-os-erp",
            )
        )


async def test_multica_adapter_creates_agent_issue_and_task_run() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        if request.method == "GET" and request.url.path == "/api/agents":
            return httpx.Response(status_code=200, json=[])
        if request.method == "GET" and request.url.path == "/api/runtimes":
            return httpx.Response(
                status_code=200,
                json=[
                    {"id": "runtime-hermes", "provider": "hermes", "status": "online"},
                    {"id": "runtime-codex", "provider": "codex", "status": "online"},
                ],
            )
        if request.method == "POST" and request.url.path == "/api/agents":
            return httpx.Response(
                status_code=201,
                json={
                    "id": "agent-hermes",
                    "name": "agentic-sdlc-hermes",
                    "runtime_id": "runtime-hermes",
                    "status": "online",
                },
            )
        if request.method == "POST" and request.url.path == "/api/issues":
            return httpx.Response(
                status_code=201,
                json={"id": "issue-1", "status": "todo", "key": "ASDLC-1"},
            )
        if request.method == "GET" and request.url.path == "/api/issues/issue-1/task-runs":
            return httpx.Response(
                status_code=200,
                json=[
                    {
                        "id": "multica-task-1",
                        "status": "queued",
                        "agent_id": "agent-hermes",
                        "runtime_id": "runtime-hermes",
                        "issue_id": "issue-1",
                    }
                ],
            )
        return httpx.Response(status_code=404, json={"error": "unexpected request"})

    orchestrator = MulticaTaskOrchestrator(
        multica_settings(),
        transport=httpx.MockTransport(handler),
    )

    response = await orchestrator.create_task(
        TaskRequest(
            source="linear",
            external_id="OS-1284",
            title="Build webhook bridge",
            repo="keychain-os-erp",
            inbound_event_id="event-1",
            metadata={
                "dag_id": "dag-1",
                "node_key": "design",
                "expected_branch": "agent/dag/dag-1/design",
                "runtime_provider": "hermes",
            },
        )
    )

    assert response.external_task_id == "multica-task-1"
    assert response.status == "queued"
    assert response.metadata is not None
    llm_observability = response.metadata.pop("llm_observability")
    assert llm_observability["operation"] == "multica.create_task.description"
    assert llm_observability["model"] == "hermes"
    assert llm_observability["input_tokens"] > 0
    assert llm_observability["estimated_cost_usd"] > 0
    assert response.metadata == {
        "multica_issue_id": "issue-1",
        "multica_issue_status": "todo",
        "multica_issue_key": "ASDLC-1",
        "multica_task_id": "multica-task-1",
        "multica_task_status": "queued",
        "multica_agent_id": "agent-hermes",
        "multica_agent_name": "agentic-sdlc-hermes",
        "multica_runtime_id": "runtime-hermes",
        "multica_runtime_provider": "hermes",
        "multica_workspace_id": "workspace-1",
    }

    issue_request = next(
        request for request in captured_requests if request.url.path == "/api/issues"
    )
    assert issue_request.headers["authorization"] == "Bearer test-key"
    assert issue_request.headers["x-workspace-id"] == "workspace-1"
    assert issue_request.headers["x-client-platform"] == "agentic-sdlc-platform"
    issue_payload = json.loads(issue_request.content)
    assert issue_payload["assignee_type"] == "agent"
    assert issue_payload["assignee_id"] == "agent-hermes"
    assert issue_payload["title"] == "Build webhook bridge"
    assert "agent/dag/dag-1/design" in issue_payload["description"]


async def test_multica_adapter_reuses_existing_agent_for_provider_runtime() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and request.url.path == "/api/agents":
            return httpx.Response(
                status_code=200,
                json=[
                    {
                        "id": "agent-codex",
                        "name": "agentic-sdlc-codex",
                        "runtime_id": "runtime-codex",
                        "status": "online",
                    }
                ],
            )
        if request.method == "GET" and request.url.path == "/api/runtimes":
            return httpx.Response(
                status_code=200,
                json=[{"id": "runtime-codex", "provider": "codex", "status": "online"}],
            )
        if request.method == "POST" and request.url.path == "/api/issues":
            return httpx.Response(status_code=201, json={"id": "issue-1", "status": "todo"})
        if request.method == "GET" and request.url.path == "/api/issues/issue-1/task-runs":
            return httpx.Response(
                status_code=200,
                json=[
                    {
                        "id": "multica-task-1",
                        "status": "queued",
                        "agent_id": "agent-codex",
                        "runtime_id": "runtime-codex",
                        "issue_id": "issue-1",
                    }
                ],
            )
        return httpx.Response(status_code=404, json={"error": "unexpected request"})

    orchestrator = MulticaTaskOrchestrator(
        multica_settings(),
        transport=httpx.MockTransport(handler),
    )

    response = await orchestrator.create_task(
        TaskRequest(
            source="dag",
            external_id="dag-1:api",
            title="Implement API",
            repo="keychain-os-erp",
        )
    )

    assert response.external_task_id == "multica-task-1"
    assert "POST /api/agents" not in requested_paths


async def test_multica_adapter_reads_task_from_issue_task_runs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/issues/issue-1/task-runs"
        return httpx.Response(
            status_code=200,
            json=[
                {
                    "id": "multica-task-1",
                    "status": "running",
                    "agent_id": "agent-codex",
                    "runtime_id": "runtime-codex",
                    "issue_id": "issue-1",
                    "attempt": 2,
                }
            ],
        )

    orchestrator = MulticaTaskOrchestrator(
        multica_settings(),
        transport=httpx.MockTransport(handler),
    )

    response = await orchestrator.read_task(
        TaskReadRequest(
            external_task_id="multica-task-1",
            metadata={"multica_issue_id": "issue-1"},
        )
    )

    assert response.external_task_id == "multica-task-1"
    assert response.status == "running"
    assert response.metadata == {
        "multica_issue_id": "issue-1",
        "multica_task_id": "multica-task-1",
        "multica_task_status": "running",
        "multica_agent_id": "agent-codex",
        "multica_runtime_id": "runtime-codex",
        "multica_attempt": 2,
    }


async def test_multica_adapter_adds_followup_comment_to_issue() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(status_code=201, json={"id": "comment-1"})

    orchestrator = MulticaTaskOrchestrator(
        multica_settings(),
        transport=httpx.MockTransport(handler),
    )

    response = await orchestrator.add_comment(
        TaskCommentRequest(
            external_task_id="multica-task-1",
            body="Can you explain the exact class?",
            actor="linear:user-1",
            metadata={"multica_issue_id": "issue-1"},
        )
    )

    assert response.external_task_id == "multica-task-1"
    assert response.comment_id == "comment-1"
    assert response.status == "commented"
    assert response.metadata == {
        "multica_issue_id": "issue-1",
        "multica_comment_id": "comment-1",
        "multica_comment_actor": "linear:user-1",
    }
    assert captured_request is not None
    assert str(captured_request.url) == "https://multica.local/api/issues/issue-1/comments"
    assert json.loads(captured_request.content) == {
        "type": "comment",
        "content": "Can you explain the exact class?",
    }


async def test_multica_adapter_posts_status_update_comment_when_issue_is_known() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(status_code=201, json={"id": "comment-1"})

    orchestrator = MulticaTaskOrchestrator(
        multica_settings(),
        transport=httpx.MockTransport(handler),
    )

    response = await orchestrator.update_task(
        TaskUpdateRequest(
            external_task_id="multica-task-1",
            status="pr_open",
            metadata={
                "multica_issue_id": "issue-1",
                "dag_id": "dag-1",
                "node_key": "api",
                "pr_number": 17,
            },
        )
    )

    assert response.external_task_id == "multica-task-1"
    assert response.status == "pr_open"
    assert response.metadata == {
        "multica_issue_id": "issue-1",
        "multica_update_synced": True,
    }
    assert captured_request is not None
    assert captured_request.method == "POST"
    assert str(captured_request.url) == "https://multica.local/api/issues/issue-1/comments"
    assert json.loads(captured_request.content) == {
        "type": "comment",
        "content": (
            "agentic-sdlc-platform updated task `multica-task-1` to `pr_open`.\n\n"
            "```json\n"
            '{\n  "dag_id": "dag-1",\n  "multica_issue_id": "issue-1",\n'
            '  "node_key": "api",\n  "pr_number": 17\n}\n'
            "```"
        ),
    }


async def test_multica_adapter_requires_workspace_id_for_real_api() -> None:
    orchestrator = MulticaTaskOrchestrator(
        Settings(
            multica_http_enabled=True,
            multica_base_url="https://multica.local",
            multica_api_key="test-key",
            multica_workspace_id=None,
        ),
        transport=httpx.MockTransport(lambda request: httpx.Response(status_code=200)),
    )

    with pytest.raises(TaskOrchestratorError, match="multica workspace ID is not configured"):
        await orchestrator.create_task(
            TaskRequest(
                source="linear",
                external_id="OS-1284",
                title="Build webhook bridge",
            )
        )


async def test_multica_adapter_raises_when_runtime_provider_is_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/agents":
            return httpx.Response(status_code=200, json=[])
        if request.method == "GET" and request.url.path == "/api/runtimes":
            return httpx.Response(
                status_code=200,
                json=[{"id": "runtime-codex", "provider": "codex", "status": "online"}],
            )
        return httpx.Response(status_code=404)

    orchestrator = MulticaTaskOrchestrator(
        multica_settings(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(TaskOrchestratorError, match="no multica runtime"):
        await orchestrator.create_task(
            TaskRequest(
                source="dag",
                external_id="dag-1:research",
                title="Research with Hermes",
                metadata={"runtime_provider": "hermes"},
            )
        )


async def test_multica_adapter_raises_structured_error_for_api_failure() -> None:
    orchestrator = MulticaTaskOrchestrator(
        multica_settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status_code=500, json={"error": "boom"})
        ),
    )

    with pytest.raises(TaskOrchestratorError, match="multica list_agents failed"):
        await orchestrator.create_task(
            TaskRequest(
                source="github",
                external_id="GunnerBot/agentic-sdlc-platform#42",
                title="Add channel router",
                repo="GunnerBot/agentic-sdlc-platform",
            )
        )


async def test_multica_adapter_retries_transient_failure() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(status_code=503, json={"error": "try again"})
        return httpx.Response(status_code=200, json=[])

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    orchestrator = MulticaTaskOrchestrator(
        multica_settings(multica_max_retries=2, multica_retry_backoff_seconds=0.25),
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )

    with pytest.raises(TaskOrchestratorError, match="no multica runtime"):
        await orchestrator.create_task(
            TaskRequest(
                source="linear",
                external_id="OS-1284",
                title="Build webhook bridge",
                repo="keychain-os-erp",
            )
        )

    assert attempts == 4
    assert sleeps == [0.25, 0.5]


async def test_multica_adapter_does_not_retry_permanent_failure() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status_code=400, json={"error": "bad request"})

    orchestrator = MulticaTaskOrchestrator(
        multica_settings(multica_max_retries=2),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(TaskOrchestratorError, match="multica list_agents failed"):
        await orchestrator.create_task(
            TaskRequest(
                source="linear",
                external_id="OS-1284",
                title="Build webhook bridge",
            )
        )

    assert attempts == 1
