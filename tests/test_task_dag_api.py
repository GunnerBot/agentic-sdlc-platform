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
from agentic_sdlc_platform.ports.issue_tracker import IssueContext, IssueTrackerReply
from agentic_sdlc_platform.ports.model_provider import ModelRequest, ModelResponse
from agentic_sdlc_platform.ports.task_orchestrator import (
    TaskConversationMessage,
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

    def __init__(
        self,
        *,
        read_status: str = "running",
        read_metadata: dict[str, object] | None = None,
    ) -> None:
        self.requests: list[TaskRequest] = []
        self.read_requests: list[TaskReadRequest] = []
        self.read_status = read_status
        self.read_metadata = read_metadata or {
            "multica_task_status": read_status,
            "multica_runtime_provider": "codex",
        }
        self.comments = [
            TaskConversationMessage(
                id="multica-comment-1",
                body="Agent found the answer.",
                actor="agent",
                metadata={"multica_issue_id": "issue-1"},
            )
        ]

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
            status=self.read_status,
            metadata=self.read_metadata,
        )

    async def list_comments(self, external_task_id: str, metadata=None):
        return self.comments


class FakeIssueTracker:
    def __init__(self) -> None:
        self.replies: list[IssueTrackerReply] = []
        self.context_requests: list[str] = []

    async def reply(self, reply: IssueTrackerReply) -> None:
        self.replies.append(reply)

    async def get_issue_context(self, issue_id: str) -> IssueContext:
        self.context_requests.append(issue_id)
        return IssueContext(
            issue_id="linear-issue-id-1",
            identifier=issue_id,
            title="BE: Add customer PO number to SO",
            description=(
                "Need a Customer PO# alphanumeric field on SO. "
                "It is non-mandatory and used across invoices and BOL. "
                "Repo: erp-service."
            ),
            url=f"https://linear.app/acme/issue/{issue_id.lower()}",
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
        external_id="ENG-1284",
        title="Build agentic SDLC platform",
        repo="erp-service",
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
            "title": "Design implementation for ENG-1284",
            "repo": "erp-service",
            "depends_on": [],
            "status": "ready",
        },
        {
            "node_key": "contract",
            "title": "Define contracts for ENG-1284",
            "repo": "erp-service",
            "depends_on": ["design"],
            "status": "blocked",
        },
        {
            "node_key": "implement",
            "title": "Implement ENG-1284",
            "repo": "erp-service",
            "depends_on": ["contract"],
            "status": "blocked",
        },
        {
            "node_key": "verify",
            "title": "Verify ENG-1284",
            "repo": "erp-service",
            "depends_on": ["implement"],
            "status": "blocked",
        },
        {
            "node_key": "review",
            "title": "Review and prepare PR for ENG-1284",
            "repo": "erp-service",
            "depends_on": ["verify"],
            "status": "blocked",
        },
    ]


async def test_create_task_dag_hydrates_linear_context_and_uses_generic_fallback() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(multica_http_enabled=False),
            repository=repository,
            issue_tracker=issue_tracker,
        )
    )

    response = client.post(
        f"/tasks/{task_id}/dag",
        json={"spec_markdown": "Please plan this Linear issue."},
    )
    artifacts = client.get(f"/tasks/{task_id}/artifacts", params={"kind": "hydrated_spec"})

    assert response.status_code == 201
    assert issue_tracker.context_requests == ["ENG-1284"]
    assert [node["node_key"] for node in response.json()["nodes"]] == [
        "backend_data_model_api",
    ]
    assert response.json()["nodes"][0]["repo"] == "erp-service"
    assert response.json()["nodes"][0]["depends_on"] == []
    assert response.json()["nodes"][0]["acceptance_criteria"] == [
        (
            "Database or schema migration applies required persistence changes "
            "with the established repo pattern."
        ),
        (
            "Backend contract, domain model, persistence, and service logic "
            "implement the requested behavior."
        ),
        (
            "Create, update, read, and list paths preserve the requested behavior "
            "where applicable."
        ),
        "Relevant automated tests are included in the same PR.",
    ]
    assert artifacts.status_code == 200
    assert artifacts.json()[0]["metadata"] == {
        "provider": "linear",
        "status": "hydrated",
    }


async def test_list_tasks_endpoint_returns_task_and_session_status() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    session = await repository.create_agent_session(
        task_id=task_id,
        provider="linear",
        external_thread_id="issue-id-1",
        hermes_session_id="hermes-session-1",
        repo="erp-service",
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
    assert payload["external_id"] == "ENG-1284"
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
            "orchestrator_provider": None,
            "orchestrator_issue_id": None,
            "orchestrator_task_id": None,
            "repo": "erp-service",
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
        repo="erp-service",
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


async def test_sync_session_orchestrator_comments_records_and_mirrors_reply() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    session = await repository.create_agent_session(
        task_id=task_id,
        provider="linear",
        external_thread_id="issue-id-1",
        hermes_session_id=None,
        repo="erp-service",
        orchestrator_provider="multica",
        orchestrator_issue_id="issue-1",
        orchestrator_task_id="multica-task-1",
    )
    task_orchestrator = FakeTaskOrchestrator()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(),
            repository=repository,
            task_orchestrator=task_orchestrator,
            issue_tracker=issue_tracker,
        )
    )

    response = client.post(f"/tasks/{task_id}/sessions/{session.id}/sync-orchestrator")

    assert response.status_code == 200
    assert response.json()["events"][-1]["event_type"] == "orchestrator_reply"
    assert response.json()["events"][-1]["message"] == "Agent found the answer."
    assert response.json()["events"][-1]["metadata"] == {
        "multica_comment_id": "multica-comment-1",
        "multica_issue_id": "issue-1",
    }
    assert issue_tracker.replies == [
        IssueTrackerReply(issue_id="issue-id-1", body="Agent found the answer.")
    ]

    second = client.post(f"/tasks/{task_id}/sessions/{session.id}/sync-orchestrator")

    assert second.status_code == 200
    assert len(second.json()["events"]) == 1
    assert issue_tracker.replies == [
        IssueTrackerReply(issue_id="issue-id-1", body="Agent found the answer.")
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
            Settings(_env_file=None),
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
        "parent_external_id": "ENG-1284",
        "dag_id": dag_id,
        "node_key": "web",
        "acceptance_criteria": [],
        "dependency_node_keys": ["api"],
        "dependencies_completed": ["api"],
        "context_session_id": None,
        "hermes_session_id": None,
        "orchestrator_idempotency_key": f"{dag_id}:web:0",
        "execution_mode": "dry_run",
        "execution_policy": {
            "terminal_command_prefix": "rtk",
            "repo_context_policy": "graphstore_first_then_narrow_source_verification",
            "github_write_enabled": False,
        },
        "code_generation_policy": task_orchestrator.requests[0].metadata[
            "code_generation_policy"
        ],
        "pr_plan": {
            "planned_pr_count": 2,
            "current_pr_index": 2,
            "current_node_key": "web",
            "ordered_node_keys": ["api", "web"],
            "depends_on_prs": ["api"],
            "unlocks_prs": [],
            "ordering_strategy": "DAG dependency order, then planner order",
            "branch_pattern": "agent/dag/<dag_id>/<node_key>",
            "body_reference_pattern": "dag/<dag_id>/<node_key>",
        },
        "repo_context": {
            "status": "unavailable",
            "reason": "graph store access is disabled",
        },
    }
    assert "expected_branch" not in task_orchestrator.requests[0].metadata
    assert "expected_pr_reference" not in task_orchestrator.requests[0].metadata


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


async def test_complete_dag_node_endpoint_requires_quality_evidence_for_pr_nodes() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    client = TestClient(
        create_app(
            Settings(_env_file=None),
            repository=repository,
            model_provider=FakePlannerModel(),
        )
    )
    created = client.post(
        f"/tasks/{task_id}/dag",
        json={"spec_markdown": "# Feature\nBuild cross-repo workflow."},
    )
    dag_id = created.json()["id"]
    await repository.update_dag_node_metadata(
        dag_id=dag_id,
        node_key="api",
        metadata={
            "execution_mode": "write_pr",
            "expected_pr_reference": f"dag/{dag_id}/api",
        },
    )

    response = client.post(f"/tasks/{task_id}/dag/{dag_id}/nodes/api/complete")
    task = client.get(f"/tasks/{task_id}")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "message": "DAG node quality gate is not satisfied",
        "missing": ["test_evidence"],
    }
    node = task.json()["dags"][0]["nodes"][0]
    assert node["status"] == "ready"


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
            metadata={"acceptance_criteria": [], "multica_issue_id": "issue-1"},
        )
    ]


async def test_sync_dag_node_orchestrator_state_records_runtime_usage_once() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    task_orchestrator = FakeTaskOrchestrator(
        read_status="completed",
        read_metadata={
            "multica_task_status": "completed",
            "multica_runtime_provider": "hermes",
            "llm_observability": {
                "operation": "hermes.multica_task_execution",
                "model": "gpt-5-mini",
                "input_tokens": 1200,
                "output_tokens": 80,
                "total_tokens": 1280,
                "estimated_cost_usd": 0.00046,
                "input_cost_per_million_usd": 0.25,
                "output_cost_per_million_usd": 2.0,
                "estimation_method": "provider_usage",
            },
        },
    )
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

    first = client.post(f"/tasks/{task_id}/dag/{dag_id}/nodes/api/sync-orchestrator")
    second = client.post(f"/tasks/{task_id}/dag/{dag_id}/nodes/api/sync-orchestrator")
    usage = client.get(f"/tasks/{task_id}/llm-observability")

    assert first.status_code == 200
    assert second.status_code == 200
    assert usage.json()["total_tokens"] == 1280
    assert usage.json()["total_estimated_cost_usd"] == 0.00046
    assert usage.json()["exact_token_record_count"] == 1
    assert usage.json()["estimated_token_record_count"] == 0
    assert usage.json()["provider_cost_record_count"] == 0
    assert len(usage.json()["records"]) == 1
    assert usage.json()["records"][0]["source"] == "task_orchestrator.read_dag_node"
    assert usage.json()["records"][0]["token_count_source"] == "provider"
    assert usage.json()["records"][0]["cost_source"] == "configured_rate_estimate"
    assert usage.json()["records"][0]["cost_exact"] is False


async def test_sync_completed_node_requires_same_pr_rework_when_incomplete() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    task_orchestrator = FakeTaskOrchestrator(
        read_status="completed",
        read_metadata={
            "multica_task_status": "completed",
            "multica_runtime_provider": "hermes",
            "pr_url": "https://github.com/acme/erp-service/pull/1320",
            "pr_number": 1320,
            "result_output": (
                "Done. Opened PR https://github.com/acme/erp-service/pull/1320\n\n"
                "Next steps you may want me to take\n"
                "- Add a Liquibase changeset file for sales_orders and sales_orders_audit.\n"
                "- Add focused unit/integration tests for create/update/get/list round trips.\n"
            ),
        },
    )
    client = TestClient(
        create_app(
            Settings(),
            repository=repository,
            model_provider=FakePlannerModel(),
            task_orchestrator=task_orchestrator,
        )
    )
    dag = await repository.create_task_dag(
        task_id=task_id,
        subtasks=[
            Subtask(
                "backend_customer_po_number",
                "Implement backend customer PO number on Sales Orders",
                repo="erp-service",
                acceptance_criteria=(
                    "Liquibase migration adds nullable customer_po_number to sales_orders.",
                    "Focused tests prove create/update/get/list round trips.",
                ),
            )
        ],
    )
    await repository.mark_dag_node_orchestrated(
        dag_id=dag.id,
        node_key="backend_customer_po_number",
        orchestrator_task_id="multica-task-1",
        orchestrator_status="queued",
        metadata={"multica_issue_id": "issue-1", "plan_approved": True},
    )

    response = client.post(
        f"/tasks/{task_id}/dag/{dag.id}/nodes/backend_customer_po_number/sync-orchestrator",
    )
    task = client.get(f"/tasks/{task_id}")
    task_list = client.get("/tasks")

    assert response.status_code == 200
    assert response.json()["status"] == "needs_changes"
    assert response.json()["verification_status"] == "rework_required"
    assert response.json()["follow_up_nodes"] == []
    assert (
        "Add focused unit/integration tests for create/update/get/list round trips."
        in response.json()["verification_missing"]
    )
    nodes = {node["node_key"]: node for node in task.json()["dags"][0]["nodes"]}
    assert nodes["backend_customer_po_number"]["pr_number"] == 1320
    assert len(nodes) == 1
    assert task_list.json()[0]["dags"][0]["ready_count"] == 0
    assert task_list.json()[0]["dags"][0]["first_ready_node"] is None
    assert task_orchestrator.requests == []


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
        json={"start": True, "execution_mode": "write_pr", "confirm_write_pr": True},
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
    assert agent_executor.requests[0].metadata["code_generation_policy"] == {
        "branching_model": "trunk_based_development",
        "base_branch": "trunk_or_default_branch",
        "pr_size": "small_ordered_prs",
        "common_code_change_policy": (
            "Changes to shared/common/existing behavior must be guarded by a "
            "feature flag or equivalent compatibility gate unless the task "
            "explicitly states that a breaking global change is intended."
        ),
        "feature_flag_required_for_common_code": True,
        "tests_policy": "implementation_and_relevant_tests_same_pr",
        "test_first_required": True,
        "test_first_policy": (
            "For write-capable DAG nodes, create or update relevant tests before "
            "production code edits, then keep tests and implementation in the same PR."
        ),
        "changed_file_test_gate": True,
        "contract_tests_required_for_api_changes": True,
        "open_pr_allowed_only_after_tests_passing": True,
        "completion_gate": (
            "Do not mark a node completed or fixed until unit, focused, contract-when-relevant, "
            "and configured smoke checks pass, and test evidence is persisted."
        ),
        "merge_order_policy": (
            "merge PRs in DAG dependency order; do not merge a dependent PR first"
        ),
    }
    assert agent_executor.requests[0].metadata["pr_plan"] == {
        "planned_pr_count": 2,
        "current_pr_index": 1,
        "current_node_key": "api",
        "ordered_node_keys": ["api", "web"],
        "depends_on_prs": [],
        "unlocks_prs": ["web"],
        "ordering_strategy": "DAG dependency order, then planner order",
        "branch_pattern": "agent/dag/<dag_id>/<node_key>",
        "body_reference_pattern": "dag/<dag_id>/<node_key>",
    }
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == execution_id
    assert updated.status_code == 200
    assert updated.json()["status"] == "pr_open"
    assert updated.json()["pr_number"] == 17
    assert updated.json()["metadata"]["review"] == "requested"
    artifacts = client.get(f"/tasks/{task_id}/artifacts")
    assert artifacts.status_code == 200
    artifact_kinds = [artifact["kind"] for artifact in artifacts.json()]
    assert "dag_node_execution_input" in artifact_kinds
    assert "dag_node_execution_result" in artifact_kinds
    input_artifact = next(
        artifact
        for artifact in artifacts.json()
        if artifact["kind"] == "dag_node_execution_input"
    )
    result_artifact = next(
        artifact
        for artifact in artifacts.json()
        if artifact["kind"] == "dag_node_execution_result"
        and artifact["metadata"]["status"] == "pr_open"
    )
    assert input_artifact["execution_id"] == execution_id
    assert input_artifact["content"]["pr_reference"] == f"dag/{dag_id}/api"
    assert input_artifact["content"]["metadata"]["pr_plan"]["planned_pr_count"] == 2
    assert result_artifact["execution_id"] == execution_id
    assert result_artifact["content"]["pr_url"] == "https://github.com/acme/erp-api/pull/17"


async def test_dag_node_execution_api_defaults_to_non_write_dry_run() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    agent_executor = FakeAgentExecutor()
    client = TestClient(
        create_app(
            Settings(),
            repository=repository,
            model_provider=FakePlannerModel(),
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
        json={},
    )

    assert created_execution.status_code == 201
    assert created_execution.json()["status"] == "queued"
    assert created_execution.json()["branch_name"] is None
    assert created_execution.json()["metadata"]["execution_mode"] == "dry_run"
    assert agent_executor.requests == []


async def test_dag_node_execution_api_requires_write_confirmation() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    client = TestClient(
        create_app(
            Settings(),
            repository=repository,
            model_provider=FakePlannerModel(),
            agent_executor=FakeAgentExecutor(),
        )
    )
    created = client.post(
        f"/tasks/{task_id}/dag",
        json={"spec_markdown": "# Feature\nBuild cross-repo workflow."},
    )
    dag_id = created.json()["id"]

    response = client.post(
        f"/tasks/{task_id}/dag/{dag_id}/nodes/api/executions",
        json={"start": True, "execution_mode": "write_pr"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "write_pr execution requires confirm_write_pr=true"
