from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.glue.dag_decomposer import Subtask
from agentic_sdlc_platform.persistence.models import Base, TaskDag
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.agent_executor import (
    AgentExecutionRequest,
    AgentExecutionResponse,
)
from agentic_sdlc_platform.ports.model_provider import ModelRequest, ModelResponse
from agentic_sdlc_platform.ports.task_orchestrator import (
    TaskReadRequest,
    TaskRequest,
    TaskResponse,
)


class FakePlannerModel:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            provider="fake",
            model="fake-model",
            content="""
[
  {"id": "api", "title": "Add API contract", "repo": "erp-api"},
  {"id": "web", "title": "Consume API", "repo": "erp-web", "depends_on": ["api"]}
]
""",
        )


class FakeTaskOrchestrator:
    provider = "multica"

    def __init__(self) -> None:
        self.requests: list[TaskRequest] = []
        self.read_requests: list[TaskReadRequest] = []

    async def create_task(self, request: TaskRequest) -> TaskResponse:
        self.requests.append(request)
        return TaskResponse(
            external_task_id=f"multica-{request.external_id}",
            status="queued",
        )

    async def read_task(self, request: TaskReadRequest) -> TaskResponse:
        self.read_requests.append(request)
        return TaskResponse(
            external_task_id=request.external_task_id,
            status="running",
            metadata={
                "multica_task_status": "running",
                "multica_runtime_provider": "codex",
            },
        )


class FakeAgentExecutor:
    provider = "fake-executor"

    def __init__(self) -> None:
        self.requests: list[AgentExecutionRequest] = []

    async def start_execution(
        self,
        request: AgentExecutionRequest,
    ) -> AgentExecutionResponse:
        self.requests.append(request)
        return AgentExecutionResponse(
            external_execution_id=f"exec-{request.node_key}",
            status="running",
            branch_name=request.branch_name,
            workspace_path=f"/tmp/{request.dag_id}/{request.node_key}",
            metadata={"started": True},
        )


async def build_repository() -> PersistenceRepository:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return PersistenceRepository(async_sessionmaker(engine, expire_on_commit=False))


async def create_parent_task(repository: PersistenceRepository) -> str:
    event_result = await repository.record_inbound_event(
        source="linear",
        delivery_id="delivery-1",
        event_type="Issue",
        payload={"id": "issue-1"},
    )
    task = await repository.create_task_from_event(
        event_id=event_result.event.id,
        source="linear",
        external_id="OS-1284",
        title="Build agentic SDLC platform",
        repo="keychain-os-erp",
    )
    return task.id


async def test_create_task_dag_endpoint_persists_planner_output() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    model_provider = FakePlannerModel()
    client = TestClient(
        create_app(
            Settings(multica_http_enabled=False),
            repository=repository,
            model_provider=model_provider,
        )
    )

    response = client.post(
        f"/tasks/{task_id}/dag",
        json={"spec_markdown": "# Feature\nBuild cross-repo workflow."},
    )

    assert response.status_code == 201
    assert response.json()["task_id"] == task_id
    assert [node["node_key"] for node in response.json()["nodes"]] == ["api", "web"]
    assert response.json()["nodes"][1]["depends_on"] == ["api"]
    assert model_provider.requests[0].role == "plan_agent"
    async with repository._session_factory() as session:
        dags = (await session.scalars(select(TaskDag))).all()

    assert len(dags) == 1


async def test_create_task_dag_endpoint_uses_builtin_feature_template() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    model_provider = FakePlannerModel()
    client = TestClient(
        create_app(
            Settings(multica_http_enabled=False),
            repository=repository,
            model_provider=model_provider,
        )
    )

    response = client.post(
        f"/tasks/{task_id}/dag",
        json={
            "spec_markdown": "# Feature\nBuild cross-repo workflow.",
            "template": "feature",
        },
    )

    assert response.status_code == 201
    assert model_provider.requests == []
    assert [
        {
            "node_key": node["node_key"],
            "title": node["title"],
            "repo": node["repo"],
            "depends_on": node["depends_on"],
            "status": node["status"],
        }
        for node in response.json()["nodes"]
    ] == [
        {
            "node_key": "design",
            "title": "Design implementation for OS-1284",
            "repo": "keychain-os-erp",
            "depends_on": [],
            "status": "ready",
        },
        {
            "node_key": "contract",
            "title": "Define contracts for OS-1284",
            "repo": "keychain-os-erp",
            "depends_on": ["design"],
            "status": "blocked",
        },
        {
            "node_key": "implement",
            "title": "Implement OS-1284",
            "repo": "keychain-os-erp",
            "depends_on": ["contract"],
            "status": "blocked",
        },
        {
            "node_key": "verify",
            "title": "Verify OS-1284",
            "repo": "keychain-os-erp",
            "depends_on": ["implement"],
            "status": "blocked",
        },
        {
            "node_key": "review",
            "title": "Review and prepare PR for OS-1284",
            "repo": "keychain-os-erp",
            "depends_on": ["verify"],
            "status": "blocked",
        },
    ]


async def test_list_tasks_endpoint_returns_task_and_session_status() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    session = await repository.create_agent_session(
        task_id=task_id,
        provider="linear",
        external_thread_id="issue-id-1",
        hermes_session_id="hermes-session-1",
        repo="keychain-os-erp",
    )
    await repository.record_session_event(
        session_id=session.id,
        direction="inbound",
        event_type="comment",
        actor="linear:user-1",
        message="What is the status?",
        metadata={"comment_id": "comment-1"},
    )
    dag = await repository.create_task_dag(
        task_id=task_id,
        subtasks=[
            Subtask("design", "Design implementation"),
            Subtask("implement", "Implement feature", depends_on=("design",)),
        ],
    )
    client = TestClient(create_app(Settings(), repository=repository))

    response = client.get("/tasks")

    assert response.status_code == 200
    payload = response.json()[0]
    assert payload["id"] == task_id
    assert payload["external_id"] == "OS-1284"
    assert payload["dags"][0]["id"] == dag.id
    assert payload["dags"][0]["node_count"] == 2
    assert payload["dags"][0]["ready_count"] == 1
    assert payload["dags"][0]["completed_count"] == 0
    assert payload["dags"][0]["skipped_count"] == 0
    assert payload["dags"][0]["failed_count"] == 0
    assert payload["dags"][0]["first_ready_node"]["node_key"] == "design"
    assert payload["sessions"] == [
        {
            "id": session.id,
            "provider": "linear",
            "external_thread_id": "issue-id-1",
            "hermes_session_id": "hermes-session-1",
            "repo": "keychain-os-erp",
            "status": "active",
            "context_summary": None,
            "event_count": 1,
        }
    ]


async def test_get_task_endpoint_returns_session_event_history() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    session = await repository.create_agent_session(
        task_id=task_id,
        provider="linear",
        external_thread_id="issue-id-1",
        hermes_session_id="hermes-session-1",
        repo="keychain-os-erp",
    )
    event = await repository.record_session_event(
        session_id=session.id,
        direction="outbound",
        event_type="reply",
        actor="agent",
        message="I am working on it.",
        metadata={"message_id": "message-1"},
    )
    dag = await repository.create_task_dag(
        task_id=task_id,
        subtasks=[
            Subtask("design", "Design implementation"),
        ],
    )
    client = TestClient(create_app(Settings(), repository=repository))

    response = client.get(f"/tasks/{task_id}")

    assert response.status_code == 200
    assert response.json()["id"] == task_id
    assert response.json()["dags"][0]["id"] == dag.id
    assert response.json()["dags"][0]["task_id"] == task_id
    assert response.json()["dags"][0]["nodes"][0]["node_key"] == "design"
    assert response.json()["dags"][0]["nodes"][0]["status"] == "ready"
    assert response.json()["sessions"][0]["events"] == [
        {
            "id": event.id,
            "direction": "outbound",
            "event_type": "reply",
            "actor": "agent",
            "message": "I am working on it.",
            "metadata": {"message_id": "message-1"},
        }
    ]


async def test_complete_dag_node_endpoint_returns_newly_ready_nodes() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    model_provider = FakePlannerModel()
    client = TestClient(
        create_app(
            Settings(multica_http_enabled=False),
            repository=repository,
            model_provider=model_provider,
        )
    )
    created = client.post(
        f"/tasks/{task_id}/dag",
        json={"spec_markdown": "# Feature\nBuild cross-repo workflow."},
    )
    dag_id = created.json()["id"]

    response = client.post(f"/tasks/{task_id}/dag/{dag_id}/nodes/api/complete")

    assert response.status_code == 200
    assert response.json()["completed_node"] == "api"
    assert response.json()["ready_nodes"][0]["node_key"] == "web"
    assert response.json()["ready_nodes"][0]["status"] == "ready"


async def test_complete_dag_node_endpoint_enqueues_newly_ready_nodes() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    model_provider = FakePlannerModel()
    task_orchestrator = FakeTaskOrchestrator()
    client = TestClient(
        create_app(
            Settings(),
            repository=repository,
            model_provider=model_provider,
            task_orchestrator=task_orchestrator,
        )
    )
    created = client.post(
        f"/tasks/{task_id}/dag",
        json={"spec_markdown": "# Feature\nBuild cross-repo workflow."},
    )
    dag_id = created.json()["id"]

    response = client.post(f"/tasks/{task_id}/dag/{dag_id}/nodes/api/complete")

    assert response.status_code == 200
    assert response.json()["ready_nodes"][0]["status"] == "queued"
    assert task_orchestrator.requests[0] == TaskRequest(
        source="dag",
        external_id=f"{dag_id}:web",
        title="Consume API",
        repo="erp-web",
        inbound_event_id=None,
        metadata=task_orchestrator.requests[0].metadata,
    )
    assert task_orchestrator.requests[0].metadata == {
        "parent_task_id": task_id,
        "parent_external_id": "OS-1284",
        "dag_id": dag_id,
        "node_key": "web",
        "dependency_node_keys": ["api"],
        "dependencies_completed": ["api"],
        "context_session_id": None,
        "hermes_session_id": None,
        "expected_pr_reference": f"dag/{dag_id}/web",
        "expected_branch": f"agent/dag/{dag_id}/web",
        "expected_pr_body_marker": f"dag/{dag_id}/web",
    }


async def test_complete_dag_node_endpoint_can_skip_auto_enqueue() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    model_provider = FakePlannerModel()
    task_orchestrator = FakeTaskOrchestrator()
    client = TestClient(
        create_app(
            Settings(),
            repository=repository,
            model_provider=model_provider,
            task_orchestrator=task_orchestrator,
        )
    )
    created = client.post(
        f"/tasks/{task_id}/dag",
        json={"spec_markdown": "# Feature\nBuild cross-repo workflow."},
    )
    dag_id = created.json()["id"]

    response = client.post(
        f"/tasks/{task_id}/dag/{dag_id}/nodes/api/complete",
        params={"enqueue_ready": "false"},
    )

    assert response.status_code == 200
    assert response.json()["ready_nodes"][0]["status"] == "ready"
    assert task_orchestrator.requests == []


async def test_fail_skip_and_retry_dag_node_endpoints() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    model_provider = FakePlannerModel()
    task_orchestrator = FakeTaskOrchestrator()
    client = TestClient(
        create_app(
            Settings(),
            repository=repository,
            model_provider=model_provider,
            task_orchestrator=task_orchestrator,
        )
    )
    created = client.post(
        f"/tasks/{task_id}/dag",
        json={"spec_markdown": "# Feature\nBuild cross-repo workflow."},
    )
    dag_id = created.json()["id"]

    failed = client.post(
        f"/tasks/{task_id}/dag/{dag_id}/nodes/api/fail",
        json={"error": "contract failed"},
    )
    retried = client.post(
        f"/tasks/{task_id}/dag/{dag_id}/nodes/api/retry",
        params={"enqueue": "false"},
    )
    skipped = client.post(f"/tasks/{task_id}/dag/{dag_id}/nodes/api/skip")

    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["failure_error"] == "contract failed"
    assert retried.status_code == 200
    assert retried.json()["status"] == "ready"
    assert retried.json()["retry_count"] == 1
    assert skipped.status_code == 200
    assert skipped.json()["completed_node"] == "api"
    assert skipped.json()["ready_nodes"][0]["node_key"] == "web"
    assert skipped.json()["ready_nodes"][0]["status"] == "queued"


async def test_sync_dag_node_orchestrator_state_polls_task_run() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    task_orchestrator = FakeTaskOrchestrator()
    client = TestClient(
        create_app(
            Settings(),
            repository=repository,
            model_provider=FakePlannerModel(),
            task_orchestrator=task_orchestrator,
        )
    )
    created = client.post(
        f"/tasks/{task_id}/dag",
        json={"spec_markdown": "# Feature\nBuild cross-repo workflow."},
    )
    dag_id = created.json()["id"]
    await repository.mark_dag_node_orchestrated(
        dag_id=dag_id,
        node_key="api",
        orchestrator_task_id="multica-task-1",
        orchestrator_status="queued",
        metadata={"multica_issue_id": "issue-1"},
    )

    response = client.post(
        f"/tasks/{task_id}/dag/{dag_id}/nodes/api/sync-orchestrator"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert response.json()["orchestrator_status"] == "running"
    assert response.json()["multica_runtime_provider"] == "codex"
    assert task_orchestrator.read_requests == [
        TaskReadRequest(
            external_task_id="multica-task-1",
            metadata={"multica_issue_id": "issue-1"},
        )
    ]


async def test_get_task_detail_returns_rich_dag_node_metadata() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    client = TestClient(
        create_app(
            Settings(),
            repository=repository,
            model_provider=FakePlannerModel(),
        )
    )
    created = client.post(
        f"/tasks/{task_id}/dag",
        json={"spec_markdown": "# Feature\nBuild cross-repo workflow."},
    )
    dag_id = created.json()["id"]
    await repository.mark_dag_node_orchestrated(
        dag_id=dag_id,
        node_key="api",
        orchestrator_task_id="multica-task-1",
        orchestrator_status="queued",
        metadata={
            "expected_branch": f"agent/dag/{dag_id}/api",
            "expected_pr_reference": f"dag/{dag_id}/api",
            "multica_issue_id": "issue-1",
            "multica_task_id": "multica-task-1",
            "multica_runtime_provider": "codex",
        },
    )

    response = client.get(f"/tasks/{task_id}")

    assert response.status_code == 200
    node = response.json()["dags"][0]["nodes"][0]
    assert node["orchestrator_task_id"] == "multica-task-1"
    assert node["orchestrator_status"] == "queued"
    assert node["expected_branch"] == f"agent/dag/{dag_id}/api"
    assert node["expected_pr_reference"] == f"dag/{dag_id}/api"
    assert node["multica_issue_id"] == "issue-1"
    assert node["multica_task_id"] == "multica-task-1"
    assert node["multica_runtime_provider"] == "codex"


async def test_dag_node_execution_api_starts_executor_and_tracks_updates() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    model_provider = FakePlannerModel()
    agent_executor = FakeAgentExecutor()
    client = TestClient(
        create_app(
            Settings(),
            repository=repository,
            model_provider=model_provider,
            agent_executor=agent_executor,
        )
    )
    created = client.post(
        f"/tasks/{task_id}/dag",
        json={"spec_markdown": "# Feature\nBuild cross-repo workflow."},
    )
    dag_id = created.json()["id"]

    created_execution = client.post(
        f"/tasks/{task_id}/dag/{dag_id}/nodes/api/executions",
        json={"start": True},
    )
    execution_id = created_execution.json()["id"]
    listed = client.get(f"/tasks/{task_id}/dag/{dag_id}/nodes/api/executions")
    updated = client.patch(
        f"/tasks/{task_id}/dag/{dag_id}/nodes/api/executions/{execution_id}",
        json={
            "status": "pr_open",
            "pr_url": "https://github.com/acme/erp-api/pull/17",
            "pr_number": 17,
            "metadata": {"review": "requested"},
        },
    )

    assert created_execution.status_code == 201
    assert created_execution.json()["status"] == "running"
    assert created_execution.json()["executor_provider"] == "fake-executor"
    assert created_execution.json()["external_execution_id"] == "exec-api"
    assert agent_executor.requests[0].branch_name == f"agent/dag/{dag_id}/api"
    assert agent_executor.requests[0].pr_reference == f"dag/{dag_id}/api"
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == execution_id
    assert updated.status_code == 200
    assert updated.json()["status"] == "pr_open"
    assert updated.json()["pr_number"] == 17
    assert updated.json()["metadata"]["review"] == "requested"
