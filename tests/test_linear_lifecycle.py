from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.persistence.models import AuditEvent, Base, TaskDag
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.design_context import DesignContext
from agentic_sdlc_platform.ports.document_context import DocumentContext
from agentic_sdlc_platform.ports.graph_store import GraphQuery, GraphQueryResult
from agentic_sdlc_platform.ports.hermes_session import (
    HermesSessionError,
    HermesSessionResponse,
    HermesStartSessionRequest,
)
from agentic_sdlc_platform.ports.issue_tracker import (
    IssueAttachment,
    IssueContext,
    IssueTrackerReply,
    IssueTrackerUpdate,
)
from agentic_sdlc_platform.ports.model_provider import ModelRequest, ModelResponse
from agentic_sdlc_platform.ports.task_orchestrator import (
    TaskCommentRequest,
    TaskCommentResponse,
    TaskRequest,
    TaskResponse,
)


class FakeTaskOrchestrator:
    provider = "multica"

    def __init__(
        self,
        task_ids: list[str] | None = None,
        include_multica_metadata: bool = False,
    ) -> None:
        self.created: list[TaskRequest] = []
        self.updated: list[tuple[str, str]] = []
        self.comments: list[TaskCommentRequest] = []
        self._task_ids = task_ids or ["multica-task-1"]
        self._include_multica_metadata = include_multica_metadata

    async def create_task(self, request: TaskRequest) -> TaskResponse:
        self.created.append(request)
        index = len(self.created) - 1
        external_task_id = (
            self._task_ids[index]
            if index < len(self._task_ids)
            else f"multica-task-{index + 1}"
        )
        metadata = None
        if self._include_multica_metadata:
            metadata = {
                "multica_issue_id": f"issue-{external_task_id}",
                "multica_task_id": external_task_id,
            }
        return TaskResponse(external_task_id=external_task_id, status="queued", metadata=metadata)

    async def update_task(self, request) -> TaskResponse:
        self.updated.append((request.external_task_id, request.status))
        return TaskResponse(external_task_id=request.external_task_id, status=request.status)

    async def add_comment(self, request: TaskCommentRequest) -> TaskCommentResponse:
        self.comments.append(request)
        return TaskCommentResponse(
            external_task_id=request.external_task_id,
            comment_id="multica-comment-1",
            status="commented",
            metadata={"multica_comment_id": "multica-comment-1"},
        )


def dry_run_metadata(
    *,
    external_id: str,
    issue_id: str,
    title: str,
    body: str | None = None,
    url: str | None = None,
) -> dict[str, object]:
    return {
        "execution_mode": "dry_run",
        "execution_policy": {
            "terminal_command_prefix": "rtk",
            "repo_context_policy": "graphstore_first_then_narrow_source_verification",
            "github_write_enabled": False,
        },
        "user_intent": {
            "source": "linear",
            "external_id": external_id,
            "issue_id": issue_id,
            "title": title,
            "body": body,
            "url": url,
        },
    }


class FakeIssueTracker:
    def __init__(self, hydrated_issues: dict[str, IssueContext] | None = None) -> None:
        self.updates: list[IssueTrackerUpdate] = []
        self.replies: list[IssueTrackerReply] = []
        self.hydrated_issue_ids: list[str] = []
        self._hydrated_issues = hydrated_issues or {}

    async def mark_task_queued(self, update: IssueTrackerUpdate) -> None:
        self.updates.append(update)

    async def reply(self, reply: IssueTrackerReply) -> None:
        self.replies.append(reply)

    async def get_issue_context(self, issue_id: str) -> IssueContext:
        self.hydrated_issue_ids.append(issue_id)
        return self._hydrated_issues.get(issue_id, IssueContext(issue_id=issue_id))


class FakeGraphStore:
    provider = "graphify"

    def __init__(self) -> None:
        self.queries: list[GraphQuery] = []

    async def index(self, request):
        raise NotImplementedError

    async def query(self, request: GraphQuery) -> GraphQueryResult:
        self.queries.append(request)
        return GraphQueryResult(
            provider=self.provider,
            answer="Foo DAFET dry-run validation is resolved from indexed repo context.",
            references=["apps/foo/dafet/form.ts:42"],
        )


class FakeHermesSession:
    def __init__(self) -> None:
        self.started: list[HermesStartSessionRequest] = []
        self.resumed: list[tuple[str, str, str]] = []

    async def start_session(self, request: HermesStartSessionRequest) -> HermesSessionResponse:
        self.started.append(request)
        return HermesSessionResponse(session_id="hermes-session-1", message_id="message-1")

    async def resume_session(
        self,
        session_id: str,
        text: str,
        actor: str,
    ) -> HermesSessionResponse:
        self.resumed.append((session_id, text, actor))
        return HermesSessionResponse(
            session_id=session_id,
            message_id="message-2",
            answer="I will check inventory allocation first.",
        )


class FailingHermesSession:
    async def start_session(self, request: HermesStartSessionRequest) -> HermesSessionResponse:
        raise HermesSessionError(
            "hermes start_session failed",
            usage={
                "operation": "hermes.start_session",
                "model": "gpt-5",
                "input_tokens": 12,
                "output_tokens": 0,
                "total_tokens": 12,
                "estimated_cost_usd": 0.000009,
                "input_cost_per_million_usd": 0.75,
                "output_cost_per_million_usd": 4.5,
                "estimation_method": "chars_per_token_request",
                "failed": True,
            },
        )

    async def resume_session(
        self,
        session_id: str,
        text: str,
        actor: str,
    ) -> HermesSessionResponse:
        raise HermesSessionError("hermes resume_session failed")


class FakePlanningModel:
    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(provider="fake", model="planner-test", content=self.content)


class FakeDocumentContext:
    def __init__(self, documents: dict[str, DocumentContext]) -> None:
        self.documents = documents
        self.fetched_urls: list[str] = []

    async def fetch(self, url: str) -> DocumentContext | None:
        self.fetched_urls.append(url)
        return self.documents.get(url)


class FakeDesignContext:
    def __init__(self, designs: dict[str, DesignContext]) -> None:
        self.designs = designs
        self.fetched: list[dict[str, str | None]] = []

    async def fetch(
        self,
        url: str,
        *,
        title: str | None = None,
        content_type: str | None = None,
    ) -> DesignContext | None:
        self.fetched.append(
            {
                "url": url,
                "title": title,
                "content_type": content_type,
            }
        )
        return self.designs.get(url)


async def build_repository() -> PersistenceRepository:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return PersistenceRepository(async_sessionmaker(engine, expire_on_commit=False))


async def test_linear_assigned_issue_comments_when_agent_task_is_queued() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="main",
        metadata={},
    )
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(
                linear_agent_user_id="agent-user-1",
                linear_plan_approval_required=False,
            ),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(),
            issue_tracker=issue_tracker,
        )
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
                "labels": {"nodes": [{"name": "repo:erp-service"}]},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-lifecycle-1"},
    )

    assert response.status_code == 202
    assert response.json()["task_id"] is not None
    assert issue_tracker.updates == [
        IssueTrackerUpdate(
            issue_id="issue-id-1",
            external_id="ENG-1284",
            internal_task_id=response.json()["task_id"],
            orchestrator_task_id="multica-task-1",
        )
    ]
    assert issue_tracker.replies == [
        IssueTrackerReply(
            issue_id="issue-id-1",
            body=(
                "Accepted ENG-1284.\n"
                "Repo: erp-service.\n"
                "DAG template: none.\n"
                "First DAG node queued: none.\n"
                "Commands: /status ENG-1284, /context ENG-1284, /agents ENG-1284."
            ),
        )
    ]


async def test_linear_assigned_issue_uses_registered_repo_metadata() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="develop",
        metadata={"linear_team_key": "OS"},
    )
    task_orchestrator = FakeTaskOrchestrator()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=task_orchestrator,
            issue_tracker=issue_tracker,
        )
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
                "labels": {"nodes": [{"name": "repo:erp-service"}]},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-registered-repo-1"},
    )

    assert response.status_code == 202
    assert task_orchestrator.created == [
        TaskRequest(
            source="linear",
            external_id="ENG-1284",
                title="Build webhook bridge",
                repo="erp-service",
                inbound_event_id=task_orchestrator.created[0].inbound_event_id,
                metadata={
                    **dry_run_metadata(
                        external_id="ENG-1284",
                        issue_id="issue-id-1",
                        title="Build webhook bridge",
                    ),
                    "repo_provider": "github",
                    "repo_clone_url": "https://github.com/acme-corp/erp-service.git",
                    "repo_default_branch": "develop",
                "repo_metadata": {"linear_team_key": "OS"},
                "repo_context": {
                    "status": "unavailable",
                    "reason": "graphify CLI query requires graph_path or repo local_path metadata",
                },
            },
        )
    ]
    assert issue_tracker.replies == [
        IssueTrackerReply(
            issue_id="issue-id-1",
            body=(
                "Accepted ENG-1284.\n"
                "Repo: erp-service.\n"
                "DAG template: none.\n"
                "First DAG node queued: none.\n"
                "Commands: /status ENG-1284, /context ENG-1284, /agents ENG-1284."
            ),
        )
    ]


async def test_linear_assigned_issue_includes_graphify_repo_context_when_available() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="develop",
        metadata={"linear_team_key": "OS"},
    )
    graph_store = FakeGraphStore()
    task_orchestrator = FakeTaskOrchestrator()
    client = TestClient(
        create_app(
            Settings(
                linear_agent_user_id="agent-user-1",
                linear_plan_approval_required=False,
                vendor_http_enabled=True,
                graphify_base_url="http://graphify.local",
            ),
            repository=repository,
            task_orchestrator=task_orchestrator,
            graph_store=graph_store,
            issue_tracker=FakeIssueTracker(),
        )
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Explain foo DAFET validation dry run behaviour",
                "description": "How does it work on form submission?",
                "assignee": {"id": "agent-user-1"},
                "labels": {"nodes": [{"name": "repo:erp-service"}]},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-graphify-context-1"},
    )

    assert response.status_code == 202
    assert graph_store.queries == [
        GraphQuery(
            repo="erp-service",
            question=(
                "Explain foo DAFET validation dry run behaviour\n\n"
                "How does it work on form submission?"
            ),
            metadata={"source": "linear", "external_id": "ENG-1284"},
        )
    ]
    assert task_orchestrator.created[0].metadata == {
        **dry_run_metadata(
            external_id="ENG-1284",
            issue_id="issue-id-1",
            title="Explain foo DAFET validation dry run behaviour",
            body="How does it work on form submission?",
        ),
        "repo_provider": "github",
        "repo_clone_url": "https://github.com/acme-corp/erp-service.git",
        "repo_default_branch": "develop",
        "repo_metadata": {"linear_team_key": "OS"},
        "repo_context": {
            "status": "available",
            "provider": "graphify",
            "answer": "Foo DAFET dry-run validation is resolved from indexed repo context.",
            "references": ["apps/foo/dafet/form.ts:42"],
            "answer_chars": 67,
            "original_answer_chars": 67,
            "truncated": False,
            "reference_count": 1,
            "references_truncated": False,
        },
    }


async def test_linear_assigned_issue_ingests_multirepo_markdown_spec() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="main",
        metadata={},
    )
    await repository.upsert_repo(
        name="frontend-monorepo",
        provider="github",
        clone_url="https://github.com/acme-corp/frontend-monorepo.git",
        default_branch="main",
        metadata={},
    )
    graph_store = FakeGraphStore()
    task_orchestrator = FakeTaskOrchestrator(
        task_ids=["multica-os-node", "multica-web-node"]
    )
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(
                linear_agent_user_id="agent-user-1",
                linear_plan_approval_required=False,
                vendor_http_enabled=True,
                graphify_base_url="http://graphify.local",
            ),
            repository=repository,
            task_orchestrator=task_orchestrator,
            graph_store=graph_store,
            issue_tracker=issue_tracker,
        )
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-2222",
                "title": "Support dynamic form titles",
                "description": (
                    "## Repositories\n"
                    "- erp-service\n"
                    "- frontend-monorepo\n\n"
                    "## Acceptance\n"
                    "- Backend supplies title metadata.\n"
                    "- Webapp renders dynamic titles."
                ),
                "assignee": {"id": "agent-user-1"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-multirepo-spec-1"},
    )

    assert response.status_code == 202
    async with repository._session_factory() as session:
        dag = (
            await session.scalars(select(TaskDag).options(selectinload(TaskDag.nodes)))
        ).one()
        spec_event = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "task.spec_ingested")
            )
        ).one()

    assert [node.node_key for node in dag.nodes] == [
        "scope_erp_service",
        "scope_frontend_monorepo",
    ]
    assert [node.repo for node in dag.nodes] == ["erp-service", "frontend-monorepo"]
    assert [request.repo for request in task_orchestrator.created] == [
        "erp-service",
        "frontend-monorepo",
    ]
    assert task_orchestrator.created[0].metadata["spec_ingestion"]["repo_scope"] == {
        "scope": "multi_repo",
        "repos": [
            {"repo": "erp-service", "reason": "mentioned_in_spec"},
            {"repo": "frontend-monorepo", "reason": "mentioned_in_spec"},
        ],
        "unknown_repos": [],
    }
    assert task_orchestrator.created[1].metadata["spec_ingestion"]["repo_scope"][
        "scope"
    ] == "multi_repo"
    assert [query.repo for query in graph_store.queries] == [
        "erp-service",
        "frontend-monorepo",
    ]
    assert spec_event.metadata_json["repo_scope"]["scope"] == "multi_repo"
    assert issue_tracker.replies == [
        IssueTrackerReply(
            issue_id="issue-id-1",
            body=(
                "Accepted ENG-2222.\n"
                "Repo: none.\n"
                "Spec repo scope: multi_repo (erp-service, frontend-monorepo).\n"
                "DAG template: linear-spec.\n"
                "Planned nodes: 2 (scope_erp_service, scope_frontend_monorepo).\n"
                "First DAG node queued: scope_erp_service (queued).\n"
                "Commands: /status ENG-2222, /context ENG-2222, /agents ENG-2222."
            ),
        )
    ]


async def test_linear_assigned_issue_uses_model_planner_for_spec_dag() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="main",
        metadata={},
    )
    await repository.upsert_repo(
        name="frontend-monorepo",
        provider="github",
        clone_url="https://github.com/acme-corp/frontend-monorepo.git",
        default_branch="main",
        metadata={},
    )
    graph_store = FakeGraphStore()
    planning_model = FakePlanningModel(
        """
[
  {
    "id": "backend_contract",
    "title": "Add dynamic title contract",
    "repo": "erp-service"
  },
  {
    "id": "backend_impl",
    "title": "Persist and expose dynamic form title metadata",
    "repo": "erp-service",
    "depends_on": ["backend_contract"]
  },
  {
    "id": "frontend_impl",
    "title": "Render dynamic form titles in the web app",
    "repo": "frontend-monorepo",
    "depends_on": ["backend_contract"]
  }
]
"""
    )
    task_orchestrator = FakeTaskOrchestrator(task_ids=["multica-contract-node"])
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(
                linear_agent_user_id="agent-user-1",
                linear_spec_planner_enabled=True,
                linear_plan_approval_required=False,
                vendor_http_enabled=True,
                model_provider="openai",
                openai_api_key="test-key",
                graphify_base_url="http://graphify.local",
            ),
            repository=repository,
            task_orchestrator=task_orchestrator,
            graph_store=graph_store,
            model_provider=planning_model,
            issue_tracker=issue_tracker,
        )
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-2444",
                "title": "Support dynamic form titles",
                "description": (
                    "## Repositories\n"
                    "- erp-service\n"
                    "- frontend-monorepo\n\n"
                    "## Acceptance\n"
                    "- Backend supplies title metadata.\n"
                    "- Webapp renders dynamic titles."
                ),
                "assignee": {"id": "agent-user-1"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-model-planner-1"},
    )

    assert response.status_code == 202
    async with repository._session_factory() as session:
        dag = (
            await session.scalars(select(TaskDag).options(selectinload(TaskDag.nodes)))
        ).one()
        planned_event = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "task.dag_planned")
            )
        ).one()

    assert [node.node_key for node in dag.nodes] == [
        "backend_contract",
        "backend_impl",
        "frontend_impl",
    ]
    assert [node.repo for node in dag.nodes] == [
        "erp-service",
        "erp-service",
        "frontend-monorepo",
    ]
    assert dag.nodes[0].status == "queued"
    assert dag.nodes[1].status == "blocked"
    assert dag.nodes[2].status == "blocked"
    assert task_orchestrator.created[0].external_id == f"{dag.id}:backend_contract"
    assert task_orchestrator.created[0].metadata["planning_strategy"] == "model"
    assert planned_event.metadata_json["strategy"] == "model"
    assert planned_event.metadata_json["node_count"] == 3
    assert [request.role for request in planning_model.requests] == ["plan_agent"]
    assert "erp-service" in planning_model.requests[0].prompt
    assert "frontend-monorepo" in planning_model.requests[0].prompt
    assert [query.repo for query in graph_store.queries[:2]] == [
        "erp-service",
        "frontend-monorepo",
    ]
    assert issue_tracker.replies == [
        IssueTrackerReply(
            issue_id="issue-id-1",
            body=(
                "Accepted ENG-2444.\n"
                "Repo: none.\n"
                "Spec repo scope: multi_repo (erp-service, frontend-monorepo).\n"
                "DAG template: linear-spec.\n"
                "Planned nodes: 3 (backend_contract, backend_impl, frontend_impl).\n"
                "First DAG node queued: backend_contract (queued).\n"
                "Commands: /status ENG-2444, /context ENG-2444, /agents ENG-2444."
            ),
        )
    ]


async def test_linear_model_planner_falls_back_when_plan_references_unknown_repo() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="main",
        metadata={},
    )
    planning_model = FakePlanningModel(
        """
[
  {
    "id": "unknown_repo_impl",
    "title": "Implement in unknown repo",
    "repo": "missing-repo"
  }
]
"""
    )
    task_orchestrator = FakeTaskOrchestrator(task_ids=["multica-fallback-node"])
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(
                linear_agent_user_id="agent-user-1",
                linear_spec_planner_enabled=True,
                linear_plan_approval_required=False,
                vendor_http_enabled=True,
                model_provider="openai",
                openai_api_key="test-key",
            ),
            repository=repository,
            task_orchestrator=task_orchestrator,
            model_provider=planning_model,
            issue_tracker=issue_tracker,
        )
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-2445",
                "title": "Support dynamic form titles",
                "description": (
                    "## Repositories\n"
                    "- erp-service\n\n"
                    "## Acceptance\n"
                    "- Backend supplies title metadata."
                ),
                "assignee": {"id": "agent-user-1"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-model-planner-fallback-1"},
    )

    assert response.status_code == 202
    async with repository._session_factory() as session:
        dag = (
            await session.scalars(select(TaskDag).options(selectinload(TaskDag.nodes)))
        ).one()
        planned_event = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "task.dag_planned")
            )
        ).one()

    assert [node.node_key for node in dag.nodes] == ["scope_erp_service"]
    assert [node.repo for node in dag.nodes] == ["erp-service"]
    assert planned_event.metadata_json["strategy"] == "repo_fallback"
    assert planned_event.metadata_json["fallback_reason"] == "invalid_model_plan"
    assert task_orchestrator.created[0].metadata["planning_strategy"] == "repo_fallback"


async def test_linear_model_planner_falls_back_when_plan_has_dependency_cycle() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="main",
        metadata={},
    )
    planning_model = FakePlanningModel(
        """
[
  {
    "id": "api_contract",
    "title": "Define API contract",
    "repo": "erp-service",
    "depends_on": ["api_impl"]
  },
  {
    "id": "api_impl",
    "title": "Implement API",
    "repo": "erp-service",
    "depends_on": ["api_contract"]
  }
]
"""
    )
    task_orchestrator = FakeTaskOrchestrator(task_ids=["multica-fallback-node"])
    client = TestClient(
        create_app(
            Settings(
                linear_agent_user_id="agent-user-1",
                linear_spec_planner_enabled=True,
                linear_plan_approval_required=False,
                vendor_http_enabled=True,
                model_provider="openai",
                openai_api_key="test-key",
            ),
            repository=repository,
            task_orchestrator=task_orchestrator,
            model_provider=planning_model,
            issue_tracker=FakeIssueTracker(),
        )
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-2447",
                "title": "Support dynamic form titles",
                "description": (
                    "## Repositories\n"
                    "- erp-service\n\n"
                    "## Acceptance\n"
                    "- Backend supplies title metadata."
                ),
                "assignee": {"id": "agent-user-1"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-model-planner-cycle-1"},
    )

    assert response.status_code == 202
    async with repository._session_factory() as session:
        dag = (
            await session.scalars(select(TaskDag).options(selectinload(TaskDag.nodes)))
        ).one()
        planned_event = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "task.dag_planned")
            )
        ).one()

    assert [node.node_key for node in dag.nodes] == ["scope_erp_service"]
    assert planned_event.metadata_json["strategy"] == "repo_fallback"
    assert planned_event.metadata_json["fallback_reason"] == "invalid_model_plan"
    assert task_orchestrator.created[0].metadata["planning_strategy"] == "repo_fallback"


async def test_linear_spec_plan_waits_for_approval_before_node_execution() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="main",
        metadata={},
    )
    planning_model = FakePlanningModel(
        """
[
  {
    "id": "backend_contract",
    "title": "Add dynamic title contract",
    "repo": "erp-service"
  }
]
"""
    )
    task_orchestrator = FakeTaskOrchestrator(task_ids=["multica-contract-node"])
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(
                linear_agent_user_id="agent-user-1",
                linear_spec_planner_enabled=True,
                linear_plan_approval_required=True,
                vendor_http_enabled=True,
                model_provider="openai",
                openai_api_key="test-key",
            ),
            repository=repository,
            task_orchestrator=task_orchestrator,
            model_provider=planning_model,
            issue_tracker=issue_tracker,
        )
    )

    assignment_response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-2446",
                "title": "Support dynamic form titles",
                "description": (
                    "## Repositories\n"
                    "- erp-service\n\n"
                    "## Acceptance\n"
                    "- Backend supplies title metadata."
                ),
                "assignee": {"id": "agent-user-1"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-plan-approval-1"},
    )

    task = await repository.find_task_by_external_id("ENG-2446")
    assert assignment_response.status_code == 202
    assert task is not None
    assert task.status == "needs_plan_approval"
    assert task_orchestrator.created == []
    assert issue_tracker.replies[-1] == IssueTrackerReply(
        issue_id="issue-id-1",
        body=(
            "Accepted ENG-2446.\n"
            "Repo: erp-service.\n"
            "Spec repo scope: single_repo (erp-service).\n"
            "DAG template: linear-spec.\n"
            "Planned nodes: 1 (backend_contract).\n"
            "Plan approval required: reply /approve-plan ENG-2446 to start.\n"
            "First DAG node queued: none.\n"
            "Commands: /status ENG-2446, /context ENG-2446, /agents ENG-2446."
        ),
    )

    approval_response = client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-1",
                "body": "/approve-plan ENG-2446",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "ENG-2446"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-plan-approval-2"},
    )

    assert approval_response.status_code == 202
    task = await repository.find_task_by_external_id("ENG-2446")
    assert task is not None
    assert task.status == "queued"
    assert [request.external_id for request in task_orchestrator.created] == [
        f"{task.dags[0].id}:backend_contract",
    ]
    assert task_orchestrator.created[0].metadata["plan_approved"] is True
    assert task_orchestrator.created[0].metadata["execution_mode"] == "write_pr"
    assert task_orchestrator.created[0].metadata["expected_branch"] == (
        f"agent/dag/{task.dags[0].id}/backend_contract"
    )
    assert task_orchestrator.created[0].metadata["expected_pr_reference"] == (
        f"dag/{task.dags[0].id}/backend_contract"
    )
    assert task_orchestrator.created[0].metadata["execution_policy"] == {
        "terminal_command_prefix": "rtk",
        "repo_context_policy": "graphstore_first_then_narrow_source_verification",
        "github_write_enabled": True,
    }
    assert issue_tracker.replies[-1] == IssueTrackerReply(
        issue_id="issue-id-1",
        body="Plan approved for ENG-2446. Queued nodes: backend_contract.",
    )


async def test_linear_assigned_issue_ingests_design_assets_for_hermes_context() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="frontend-monorepo",
        provider="github",
        clone_url="https://github.com/acme-corp/frontend-monorepo.git",
        default_branch="main",
        metadata={},
    )
    hermes_session = FakeHermesSession()
    task_orchestrator = FakeTaskOrchestrator()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(
                linear_agent_user_id="agent-user-1",
                linear_plan_approval_required=False,
            ),
            repository=repository,
            task_orchestrator=task_orchestrator,
            hermes_session=hermes_session,
            issue_tracker=issue_tracker,
        )
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "WEB-991",
                "title": "Build dynamic form title designs",
                "description": (
                    "## Repositories\n"
                    "- frontend-monorepo\n\n"
                    "## Design\n"
                    "Use https://www.figma.com/file/abc123/form-title-flow"
                ),
                "assignee": {"id": "agent-user-1"},
                "attachments": {
                    "nodes": [
                        {
                            "id": "attachment-1",
                            "title": "form-title-mobile.png",
                            "url": "https://linear.local/form-title-mobile.png",
                            "contentType": "image/png",
                        },
                        {
                            "id": "attachment-2",
                            "title": "frontend-notes.md",
                            "content": "## Repositories\n- frontend-monorepo",
                            "contentType": "text/markdown",
                        },
                    ]
                },
            },
        },
        headers={"Linear-Delivery": "delivery-linear-design-spec-1"},
    )

    assert response.status_code == 202
    assert hermes_session.started[0] == HermesStartSessionRequest(
        task_id=response.json()["task_id"],
        provider="linear",
        external_thread_id="issue-id-1",
        text=(
            "Build dynamic form title designs\n\n"
            "## Repositories\n"
            "- frontend-monorepo\n\n"
            "## Design\n"
            "Use https://www.figma.com/file/abc123/form-title-flow\n\n"
            "Ingested Linear spec context:\n"
            "Repos: frontend-monorepo\n"
            "Design assets:\n"
            "- form-title-mobile.png (https://linear.local/form-title-mobile.png)\n"
            "- Figma link (https://www.figma.com/file/abc123/form-title-flow)"
        ),
        repo="frontend-monorepo",
    )
    assert task_orchestrator.created[0].metadata["spec_ingestion"]["asset_count"] == 2
    assert issue_tracker.replies == [
        IssueTrackerReply(
            issue_id="issue-id-1",
            body=(
                "Accepted WEB-991.\n"
                "Repo: frontend-monorepo.\n"
                "Spec repo scope: single_repo (frontend-monorepo).\n"
                "Design assets: 2.\n"
                "DAG template: none.\n"
                "First DAG node queued: none.\n"
                "Commands: /status WEB-991, /context WEB-991, /agents WEB-991."
            ),
        )
    ]


async def test_linear_assigned_issue_hydrates_missing_spec_from_linear() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="main",
        metadata={},
    )
    await repository.upsert_repo(
        name="frontend-monorepo",
        provider="github",
        clone_url="https://github.com/acme-corp/frontend-monorepo.git",
        default_branch="main",
        metadata={},
    )
    task_orchestrator = FakeTaskOrchestrator(
        task_ids=["multica-os-node", "multica-web-node"]
    )
    issue_tracker = FakeIssueTracker(
        hydrated_issues={
            "issue-id-1": IssueContext(
                issue_id="issue-id-1",
                identifier="ENG-3001",
                title="Support dynamic form titles",
                description=(
                    "## Repositories\n"
                    "- erp-service\n"
                    "- frontend-monorepo\n\n"
                    "## Acceptance\n"
                    "- Backend persists the title expression.\n"
                    "- Webapp renders the resolved title."
                ),
                attachments=[
                    IssueAttachment(
                        id="attachment-1",
                        title="form-title-flow.png",
                        url="https://linear.local/form-title-flow.png",
                        content_type="image/png",
                    )
                ],
            )
        }
    )
    client = TestClient(
        create_app(
            Settings(
                linear_agent_user_id="agent-user-1",
                linear_plan_approval_required=False,
            ),
            repository=repository,
            task_orchestrator=task_orchestrator,
            issue_tracker=issue_tracker,
        )
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-3001",
                "title": "Support dynamic form titles",
                "description": "Assigned to agent.",
                "assignee": {"id": "agent-user-1"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-hydrated-spec-1"},
    )

    assert response.status_code == 202
    assert issue_tracker.hydrated_issue_ids == ["issue-id-1"]
    async with repository._session_factory() as session:
        dag = (
            await session.scalars(select(TaskDag).options(selectinload(TaskDag.nodes)))
        ).one()
        spec_event = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "task.spec_ingested")
            )
        ).one()

    assert [node.repo for node in dag.nodes] == ["erp-service", "frontend-monorepo"]
    assert task_orchestrator.created[0].metadata["spec_ingestion"]["asset_count"] == 1
    assert spec_event.metadata_json["text_sources"][0]["title"] == "Linear description"
    assert issue_tracker.replies[-1] == IssueTrackerReply(
        issue_id="issue-id-1",
        body=(
            "Accepted ENG-3001.\n"
            "Repo: none.\n"
            "Spec repo scope: multi_repo (erp-service, frontend-monorepo).\n"
            "Design assets: 1.\n"
            "DAG template: linear-spec.\n"
            "Planned nodes: 2 (scope_erp_service, scope_frontend_monorepo).\n"
            "First DAG node queued: scope_erp_service (queued).\n"
            "Commands: /status ENG-3001, /context ENG-3001, /agents ENG-3001."
        ),
    )


async def test_linear_assigned_issue_hydrates_notion_doc_from_description() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="main",
        metadata={},
    )
    notion_url = (
        "https://acme.notion.site/Dynamic-form-titles-"
        "1234567890abcdef1234567890abcdef"
    )
    document_context = FakeDocumentContext(
        {
            notion_url: DocumentContext(
                provider="notion",
                url=notion_url,
                title="Dynamic form titles spec",
                text=(
                    "## Repositories\n"
                    "- erp-service\n\n"
                    "## Acceptance\n"
                    "- Backend persists title expression metadata."
                ),
                metadata={"page_id": "1234567890abcdef1234567890abcdef"},
            )
        }
    )
    task_orchestrator = FakeTaskOrchestrator()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(
                linear_agent_user_id="agent-user-1",
                linear_plan_approval_required=False,
            ),
            repository=repository,
            task_orchestrator=task_orchestrator,
            document_context=document_context,
            issue_tracker=issue_tracker,
        )
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-3004",
                "title": "Support dynamic form titles",
                "description": f"Spec: {notion_url}",
                "assignee": {"id": "agent-user-1"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-notion-doc-1"},
    )

    assert response.status_code == 202
    assert document_context.fetched_urls == [notion_url]
    assert task_orchestrator.created[0].repo == "erp-service"
    assert task_orchestrator.created[0].metadata["spec_ingestion"]["repo_scope"] == {
        "scope": "single_repo",
        "repos": [{"repo": "erp-service", "reason": "mentioned_in_spec"}],
        "unknown_repos": [],
    }
    assert task_orchestrator.created[0].metadata["spec_ingestion"]["text_sources"][1] == {
        "kind": "attachment",
        "title": "Dynamic form titles spec",
        "length": 90,
    }
    async with repository._session_factory() as session:
        hydrated_event = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "linear.documents_hydrated")
            )
        ).one()
    assert hydrated_event.metadata_json["providers"] == ["notion"]


async def test_linear_assigned_issue_hydrates_figma_design_from_description() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="frontend-monorepo",
        provider="github",
        clone_url="https://github.com/acme-corp/frontend-monorepo.git",
        default_branch="main",
        metadata={},
    )
    figma_url = "https://www.figma.com/file/abc123/form-title-flow?node-id=1%3A2"
    design_context = FakeDesignContext(
        {
            figma_url: DesignContext(
                provider="figma",
                url=figma_url,
                title="Form title frame",
                summary=(
                    "Figma file: Dynamic form titles\n"
                    "Requested node: 1:2\n\n"
                    "## Repositories\n"
                    "- frontend-monorepo\n\n"
                    "## Acceptance\n"
                    "- Use the compact dynamic title frame."
                ),
                metadata={"file_key": "abc123", "node_id": "1:2"},
            )
        }
    )
    task_orchestrator = FakeTaskOrchestrator()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(
                linear_agent_user_id="agent-user-1",
                linear_plan_approval_required=False,
            ),
            repository=repository,
            task_orchestrator=task_orchestrator,
            design_context=design_context,
            issue_tracker=issue_tracker,
        )
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "WEB-3005",
                "title": "Support dynamic form titles",
                "description": f"Use this design: {figma_url}",
                "assignee": {"id": "agent-user-1"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-figma-design-1"},
    )

    assert response.status_code == 202
    assert design_context.fetched == [
        {
            "url": figma_url,
            "title": "Figma link",
            "content_type": None,
        }
    ]
    assert task_orchestrator.created[0].repo == "frontend-monorepo"
    assert task_orchestrator.created[0].metadata["spec_ingestion"]["repo_scope"] == {
        "scope": "single_repo",
        "repos": [{"repo": "frontend-monorepo", "reason": "mentioned_in_spec"}],
        "unknown_repos": [],
    }
    assert any(
        source["kind"] == "attachment"
        and source["title"] == "Form title frame"
        and source["length"] > 0
        for source in task_orchestrator.created[0].metadata["spec_ingestion"][
            "text_sources"
        ]
    )
    assert task_orchestrator.created[0].metadata["spec_ingestion"]["design_assets"] == [
        {
            "kind": "figma",
            "title": "Figma link",
            "url": figma_url,
        }
    ]
    async with repository._session_factory() as session:
        hydrated_event = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "linear.designs_hydrated")
            )
        ).one()
    assert hydrated_event.metadata_json["providers"] == ["figma"]
    assert hydrated_event.metadata_json["urls"] == [figma_url]


async def test_linear_assigned_issue_hydrates_image_attachment_summary() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="frontend-monorepo",
        provider="github",
        clone_url="https://github.com/acme-corp/frontend-monorepo.git",
        default_branch="main",
        metadata={},
    )
    image_url = "https://linear.app/attachments/form-title.png"
    design_context = FakeDesignContext(
        {
            image_url: DesignContext(
                provider="openai_vision",
                url=image_url,
                title="form-title.png",
                summary=(
                    "Image attachment: form-title.png\n\n"
                    "## Repositories\n"
                    "- frontend-monorepo\n\n"
                    "## Acceptance\n"
                    "- The dynamic form title appears above the first editable field."
                ),
                metadata={
                    "source_content_type": "image/png",
                    "byte_count": 1024,
                    "summary_provider": "openai",
                },
            )
        }
    )
    task_orchestrator = FakeTaskOrchestrator()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=task_orchestrator,
            design_context=design_context,
            issue_tracker=issue_tracker,
        )
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "WEB-3006",
                "title": "Support dynamic form title image spec",
                "description": "Please implement the attached design.",
                "assignee": {"id": "agent-user-1"},
                "attachments": {
                    "nodes": [
                        {
                            "id": "attachment-1",
                            "title": "form-title.png",
                            "url": image_url,
                            "contentType": "image/png",
                        }
                    ]
                },
            },
        },
        headers={"Linear-Delivery": "delivery-linear-image-design-1"},
    )

    assert response.status_code == 202
    assert design_context.fetched == [
        {
            "url": image_url,
            "title": "form-title.png",
            "content_type": "image/png",
        }
    ]
    assert task_orchestrator.created[0].repo == "frontend-monorepo"
    spec_ingestion = task_orchestrator.created[0].metadata["spec_ingestion"]
    assert task_orchestrator.created[0].metadata["hydrated_spec_artifact_id"]
    assert any(
        source["kind"] == "attachment"
        and source["title"] == "form-title.png"
        and source["length"] > 0
        for source in spec_ingestion["text_sources"]
    )
    assert spec_ingestion["design_assets"] == [
        {
            "kind": "image",
            "title": "form-title.png",
            "url": image_url,
            "content_type": "image/png",
        }
    ]
    artifacts = await repository.list_task_artifacts(
        task_id=response.json()["task_id"],
        kind="hydrated_spec",
    )
    assert len(artifacts) == 1
    assert artifacts[0].id == task_orchestrator.created[0].metadata[
        "hydrated_spec_artifact_id"
    ]
    assert artifacts[0].content_json["text_sources"][1]["text"] == (
        "Image attachment: form-title.png\n\n"
        "## Repositories\n"
        "- frontend-monorepo\n\n"
        "## Acceptance\n"
        "- The dynamic form title appears above the first editable field."
    )
    async with repository._session_factory() as session:
        hydrated_event = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "linear.designs_hydrated")
            )
        ).one()
    assert hydrated_event.metadata_json["providers"] == ["openai_vision"]
    assert hydrated_event.metadata_json["urls"] == [image_url]


async def test_linear_assigned_issue_blocks_and_asks_for_repo_when_spec_is_ambiguous() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="main",
        metadata={},
    )
    task_orchestrator = FakeTaskOrchestrator()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=task_orchestrator,
            issue_tracker=issue_tracker,
        )
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-3002",
                "title": "Support dynamic form titles",
                "description": (
                    "## Repositories\n"
                    "- missing-service\n\n"
                    "## Acceptance\n"
                    "- Implement the feature end to end."
                ),
                "assignee": {"id": "agent-user-1"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-ambiguous-spec-1"},
    )

    assert response.status_code == 202
    assert task_orchestrator.created == []
    task = await repository.find_task_by_external_id("ENG-3002")
    assert task is not None
    assert task.status == "blocked"
    assert issue_tracker.updates == []
    assert issue_tracker.replies == [
        IssueTrackerReply(
            issue_id="issue-id-1",
            body=(
                "I need a registered repository before I can start ENG-3002.\n"
                "Mention one of: erp-service.\n"
                "Unregistered repo mentions: missing-service."
            ),
        )
    ]


async def test_linear_repo_clarification_comment_resumes_blocked_task() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="main",
        metadata={},
    )
    task_orchestrator = FakeTaskOrchestrator()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=task_orchestrator,
            issue_tracker=issue_tracker,
        )
    )

    assignment_response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-3003",
                "title": "Support dynamic form titles",
                "description": (
                    "## Acceptance\n"
                    "- Implement the feature end to end."
                ),
                "assignee": {"id": "agent-user-1"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-clarification-resume-1"},
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-1",
                "body": "Use erp-service for this.",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "ENG-3003"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-clarification-resume-2"},
    )

    assert assignment_response.status_code == 202
    assert response.status_code == 202
    assert response.json()["task_id"] == assignment_response.json()["task_id"]
    task = await repository.find_task_by_external_id("ENG-3003")
    assert task is not None
    assert task.repo == "erp-service"
    assert task.status == "queued"
    assert task_orchestrator.created == [
        TaskRequest(
            source="linear",
            external_id="ENG-3003",
            title="Support dynamic form titles",
            repo="erp-service",
            metadata={
                "repo_provider": "github",
                "repo_clone_url": "https://github.com/acme-corp/erp-service.git",
                "repo_default_branch": "main",
                "repo_metadata": {},
                "repo_context": {
                    "status": "unavailable",
                    "reason": "graphify CLI query requires graph_path or repo local_path metadata",
                },
                "repo_clarification": {
                    "comment_id": "comment-1",
                    "actor": "linear:user-1",
                    "resolved_repo": "erp-service",
                },
            },
        )
    ]
    assert issue_tracker.updates == [
        IssueTrackerUpdate(
            issue_id="issue-id-1",
            external_id="ENG-3003",
            internal_task_id=response.json()["task_id"],
            orchestrator_task_id="multica-task-1",
        )
    ]
    assert issue_tracker.replies[-1] == IssueTrackerReply(
        issue_id="issue-id-1",
        body="Thanks, I will use erp-service and start ENG-3003.",
    )


async def test_linear_assigned_issue_with_type_label_creates_dag_template() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="main",
        metadata={},
    )
    task_orchestrator = FakeTaskOrchestrator(task_ids=["multica-dag-node-1"])
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=task_orchestrator,
            issue_tracker=issue_tracker,
        )
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
                "labels": {
                    "nodes": [
                        {"name": "repo:erp-service"},
                        {"name": "type:bug"},
                    ]
                },
            },
        },
        headers={"Linear-Delivery": "delivery-linear-dag-template-1"},
    )

    assert response.status_code == 202
    async with repository._session_factory() as session:
        dag = (
            await session.scalars(select(TaskDag).options(selectinload(TaskDag.nodes)))
        ).one()
        dag_created_event = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "task.dag_template_created")
            )
        ).one()
        dag_node_enqueued_event = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "task.dag_node_enqueued")
            )
        ).one()

        assert [node.node_key for node in dag.nodes] == ["reproduce", "fix", "test", "review"]
        assert [node.repo for node in dag.nodes] == ["erp-service"] * 4
        assert dag.nodes[0].status == "queued"
        assert dag.nodes[0].orchestrator_task_id == "multica-dag-node-1"
        assert dag.nodes[1].status == "blocked"
    assert task_orchestrator.created[0].source == "dag"
    assert task_orchestrator.created[0].external_id == f"{dag.id}:reproduce"
    assert task_orchestrator.created[0].metadata == {
        "parent_task_id": response.json()["task_id"],
        "parent_external_id": "ENG-1284",
        "dag_id": dag.id,
        "node_key": "reproduce",
        "acceptance_criteria": [],
        "dependency_node_keys": [],
        "dependencies_completed": [],
        "context_session_id": None,
        "hermes_session_id": None,
        "orchestrator_idempotency_key": f"{dag.id}:reproduce:0",
        "code_generation_policy": task_orchestrator.created[0].metadata[
            "code_generation_policy"
        ],
        "pr_plan": {
            "planned_pr_count": 4,
            "current_pr_index": 1,
            "current_node_key": "reproduce",
            "ordered_node_keys": ["reproduce", "fix", "test", "review"],
            "depends_on_prs": [],
            "unlocks_prs": ["fix"],
            "ordering_strategy": "DAG dependency order, then planner order",
            "branch_pattern": "agent/dag/<dag_id>/<node_key>",
            "body_reference_pattern": "dag/<dag_id>/<node_key>",
        },
        "repo_context": {
            "status": "unavailable",
            "reason": "graphify CLI query requires graph_path or repo local_path metadata",
        },
        **dry_run_metadata(
            external_id="ENG-1284",
            issue_id="issue-id-1",
            title="Build webhook bridge",
        ),
        "repo_provider": "github",
        "repo_clone_url": "https://github.com/acme-corp/erp-service.git",
        "repo_default_branch": "main",
        "repo_metadata": {},
    }
    assert dag_created_event.metadata_json["template"] == "bugfix"
    assert dag_created_event.metadata_json["node_count"] == 4
    assert dag_node_enqueued_event.metadata_json["node_key"] == "reproduce"
    assert dag_node_enqueued_event.metadata_json["orchestrator_task_id"] == "multica-dag-node-1"
    assert issue_tracker.replies == [
        IssueTrackerReply(
            issue_id="issue-id-1",
            body=(
                "Accepted ENG-1284.\n"
                "Repo: erp-service.\n"
                "DAG template: bugfix.\n"
                "First DAG node queued: reproduce (queued).\n"
                "Commands: /status ENG-1284, /context ENG-1284, /agents ENG-1284."
            ),
        )
    ]


async def test_linear_assigned_issue_blocks_when_repo_label_is_unknown() -> None:
    repository = await build_repository()
    task_orchestrator = FakeTaskOrchestrator()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=task_orchestrator,
            issue_tracker=issue_tracker,
        )
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
                "labels": {"nodes": [{"name": "repo:missing-repo"}]},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-unknown-repo-1"},
    )

    assert response.status_code == 202
    assert response.json()["task_id"] is not None
    assert task_orchestrator.created == []
    assert issue_tracker.updates == []
    assert issue_tracker.replies == [
        IssueTrackerReply(
            issue_id="issue-id-1",
            body=(
                "Repository missing-repo is not registered. "
                "Register it before I can work on ENG-1284."
            ),
        )
    ]
    task = await repository.find_task_by_external_id("ENG-1284")
    assert task is not None
    assert task.status == "blocked"


async def test_linear_assigned_issue_starts_and_persists_hermes_session() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="main",
        metadata={},
    )
    hermes_session = FakeHermesSession()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(),
            hermes_session=hermes_session,
            issue_tracker=FakeIssueTracker(),
        )
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "description": "Create the bridge.",
                "assignee": {"id": "agent-user-1"},
                "labels": {"nodes": [{"name": "repo:erp-service"}]},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-session-1"},
    )

    assert response.status_code == 202
    assert hermes_session.started == [
        HermesStartSessionRequest(
            task_id=response.json()["task_id"],
            provider="linear",
            external_thread_id="issue-id-1",
            text="Build webhook bridge\n\nCreate the bridge.",
            repo="erp-service",
        )
    ]
    persisted = await repository.find_agent_session(
        provider="linear",
        external_thread_id="issue-id-1",
    )
    assert persisted is not None
    assert persisted.hermes_session_id == "hermes-session-1"


async def test_linear_assigned_issue_records_hermes_start_failure_without_aborting() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="main",
        metadata={},
    )
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(include_multica_metadata=True),
            hermes_session=FailingHermesSession(),
            issue_tracker=issue_tracker,
        )
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "description": "Create the bridge.",
                "assignee": {"id": "agent-user-1"},
                "labels": {"nodes": [{"name": "repo:erp-service"}]},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-session-failure-1"},
    )

    assert response.status_code == 202
    persisted = await repository.find_agent_session(
        provider="linear",
        external_thread_id="issue-id-1",
    )
    assert persisted is not None
    assert persisted.hermes_session_id is None
    assert persisted.orchestrator_task_id == "multica-task-1"
    events = await repository.list_session_events(persisted.id)
    assert events[-1].event_type == "session_start_failed"
    assert events[-1].metadata_json["llm_observability"] == {
        "operation": "hermes.start_session",
        "model": "gpt-5",
        "input_tokens": 12,
        "output_tokens": 0,
        "total_tokens": 12,
        "estimated_cost_usd": 0.000009,
        "input_cost_per_million_usd": 0.75,
        "output_cost_per_million_usd": 4.5,
        "estimation_method": "chars_per_token_request",
        "failed": True,
    }
    observability_response = client.get(
        f"/tasks/{response.json()['task_id']}/llm-observability"
    )
    assert observability_response.status_code == 200
    observability = observability_response.json()
    assert observability["total_input_tokens"] == 12
    assert observability["total_output_tokens"] == 0
    assert observability["total_tokens"] == 12
    assert observability["total_estimated_cost_usd"] == 0.000009
    assert observability["exact_token_record_count"] == 0
    assert observability["estimated_token_record_count"] == 1
    assert observability["provider_cost_record_count"] == 0
    assert observability["records"][0]["source"] == "hermes_session.start_failed"
    assert observability["records"][0]["token_count_source"] == "estimated"
    assert observability["records"][0]["cost_source"] == "configured_rate_estimate"
    assert observability["records"][0]["cost_exact"] is False


async def test_linear_issue_assigned_to_other_user_is_not_actionable() -> None:
    repository = await build_repository()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(),
            issue_tracker=issue_tracker,
        )
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "someone-else"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-lifecycle-2"},
    )

    assert response.status_code == 202
    assert response.json()["task_id"] is None
    assert issue_tracker.updates == []


async def test_linear_comment_resumes_session_and_replies_in_thread() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="main",
        metadata={},
    )
    hermes_session = FakeHermesSession()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(),
            hermes_session=hermes_session,
            issue_tracker=issue_tracker,
        )
    )

    assignment_response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
                "labels": {"nodes": [{"name": "repo:erp-service"}]},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-session-comment-1"},
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-1",
                "body": "Please check inventory allocation first.",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "ENG-1284"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-session-comment-2"},
    )

    assert assignment_response.status_code == 202
    assert response.status_code == 202
    assert response.json()["task_id"] == assignment_response.json()["task_id"]
    assert hermes_session.resumed == [
        (
            "hermes-session-1",
            "Please check inventory allocation first.",
            "linear:user-1",
        )
    ]
    assert issue_tracker.replies[-1] == (
        IssueTrackerReply(
            issue_id="issue-id-1",
            body="I will check inventory allocation first.",
        )
    )
    persisted = await repository.find_agent_session(
        provider="linear",
        external_thread_id="issue-id-1",
    )
    assert persisted is not None
    events = await repository.list_session_events(persisted.id)
    recorded_events = [
        (event.direction, event.event_type, event.actor, event.message) for event in events
    ]
    assert recorded_events == [
        ("outbound", "session_started", "system", "Build webhook bridge"),
        ("inbound", "comment", "linear:user-1", "Please check inventory allocation first."),
        ("outbound", "reply", "agent", "I will check inventory allocation first."),
    ]


async def test_linear_comment_on_multica_backed_session_is_added_to_multica_issue() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="main",
        metadata={},
    )
    task_orchestrator = FakeTaskOrchestrator(include_multica_metadata=True)
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=task_orchestrator,
            hermes_session=None,
            issue_tracker=issue_tracker,
        )
    )

    assignment_response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
                "labels": {"nodes": [{"name": "repo:erp-service"}]},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-multica-session-1"},
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-1",
                "body": "What exact class has the dryRun default mismatch?",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "ENG-1284"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-multica-session-2"},
    )

    assert assignment_response.status_code == 202
    assert response.status_code == 202
    assert response.json()["task_id"] == assignment_response.json()["task_id"]
    assert task_orchestrator.comments == [
        TaskCommentRequest(
            external_task_id="multica-task-1",
            body="What exact class has the dryRun default mismatch?",
            actor="linear:user-1",
            metadata={
                "multica_issue_id": "issue-multica-task-1",
                "provider": "linear",
                "external_thread_id": "issue-id-1",
                "comment_id": "comment-1",
            },
        )
    ]
    persisted = await repository.find_agent_session(
        provider="linear",
        external_thread_id="issue-id-1",
    )
    assert persisted is not None
    assert persisted.orchestrator_provider == "multica"
    assert persisted.orchestrator_issue_id == "issue-multica-task-1"
    assert persisted.orchestrator_task_id == "multica-task-1"
    events = await repository.list_session_events(persisted.id)
    recorded_events = [
        (event.direction, event.event_type, event.actor, event.message) for event in events
    ]
    assert recorded_events == [
        (
            "inbound",
            "comment",
            "linear:user-1",
            "What exact class has the dryRun default mismatch?",
        ),
        (
            "outbound",
            "orchestrator_comment",
            "system",
            "What exact class has the dryRun default mismatch?",
        ),
    ]


async def test_linear_comment_from_agent_user_is_ignored_to_prevent_reply_loop() -> None:
    repository = await build_repository()
    hermes_session = FakeHermesSession()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(),
            hermes_session=hermes_session,
            issue_tracker=issue_tracker,
        )
    )

    assignment_response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-loop-1"},
    )
    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-1",
                "body": "I will check inventory allocation first.",
                "user": {"id": "agent-user-1"},
                "issue": {"id": "issue-id-1", "identifier": "ENG-1284"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-loop-2"},
    )

    assert assignment_response.status_code == 202
    assert response.status_code == 202
    assert response.json()["task_id"] == assignment_response.json()["task_id"]
    assert hermes_session.resumed == []
    assert len(issue_tracker.replies) == 1
    assert issue_tracker.replies[0].body.startswith("Accepted ENG-1284.")


async def test_linear_comment_command_updates_task_status_and_replies() -> None:
    repository = await build_repository()
    task_orchestrator = FakeTaskOrchestrator()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=task_orchestrator,
            hermes_session=FakeHermesSession(),
            issue_tracker=issue_tracker,
        )
    )

    assignment_response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-command-1"},
    )
    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-1",
                "body": "/pause ENG-1284 waiting for product decision",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "ENG-1284"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-command-2"},
    )

    assert assignment_response.status_code == 202
    assert response.status_code == 202
    assert response.json()["task_id"] == assignment_response.json()["task_id"]
    assert task_orchestrator.updated == [("multica-task-1", "paused")]
    assert issue_tracker.replies[-1] == IssueTrackerReply(
        issue_id="issue-id-1",
        body="Command /pause applied. Task ENG-1284 is now paused.",
    )
    task = await repository.find_task_by_external_id("ENG-1284")
    assert task is not None
    assert task.status == "paused"


async def test_linear_status_comment_replies_with_current_task_state() -> None:
    repository = await build_repository()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(),
            hermes_session=FakeHermesSession(),
            issue_tracker=issue_tracker,
        )
    )

    assignment_response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-status-1"},
    )
    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-1",
                "body": "/status ENG-1284",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "ENG-1284"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-status-2"},
    )

    assert assignment_response.status_code == 202
    assert response.status_code == 202
    assert response.json()["task_id"] == assignment_response.json()["task_id"]
    assert issue_tracker.replies[-1] == IssueTrackerReply(
        issue_id="issue-id-1",
        body=(
            "Task ENG-1284 status: queued. "
            "Orchestrator: multica-task-1 (queued). "
            "Repo: none. Sessions: 1 active session. "
            "DAG: none."
        ),
    )


async def test_linear_status_comment_replies_with_dag_progress() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="main",
        metadata={},
    )
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(),
            hermes_session=FakeHermesSession(),
            issue_tracker=issue_tracker,
        )
    )

    assignment_response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
                "labels": {
                    "nodes": [
                        {"name": "repo:erp-service"},
                        {"name": "type:feature"},
                    ]
                },
            },
        },
        headers={"Linear-Delivery": "delivery-linear-status-dag-1"},
    )
    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-1",
                "body": "/status ENG-1284",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "ENG-1284"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-status-dag-2"},
    )

    assert assignment_response.status_code == 202
    assert response.status_code == 202
    assert response.json()["task_id"] == assignment_response.json()["task_id"]
    assert issue_tracker.replies[-1] == IssueTrackerReply(
        issue_id="issue-id-1",
        body=(
            "Task ENG-1284 status: queued. "
            "Orchestrator: none. "
            "Repo: erp-service. Sessions: 1 active session. "
            "DAG: planned, 0/5 completed, 0 skipped, 0 failed, 0 ready, next: none."
        ),
    )


async def test_linear_nodes_comment_replies_with_dag_node_statuses() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="main",
        metadata={},
    )
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(
                linear_agent_user_id="agent-user-1",
                linear_plan_approval_required=False,
            ),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(),
            hermes_session=FakeHermesSession(),
            issue_tracker=issue_tracker,
        )
    )

    assignment_response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
                "labels": {
                    "nodes": [
                        {"name": "repo:erp-service"},
                        {"name": "type:feature"},
                    ]
                },
            },
        },
        headers={"Linear-Delivery": "delivery-linear-nodes-1"},
    )
    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-1",
                "body": "/nodes ENG-1284",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "ENG-1284"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-nodes-2"},
    )

    assert assignment_response.status_code == 202
    assert response.status_code == 202
    assert response.json()["task_id"] == assignment_response.json()["task_id"]
    assert issue_tracker.replies[-1] == IssueTrackerReply(
        issue_id="issue-id-1",
        body=(
            "Task ENG-1284 nodes:\n"
            "Next runnable: none\n"
            "- design: queued; repo erp-service; depends_on none; "
            "orchestrator multica-task-1; pr none; failure none\n"
            "- contract: blocked; repo erp-service; depends_on design; "
            "orchestrator none; pr none; failure none\n"
            "- implement: blocked; repo erp-service; depends_on contract; "
            "orchestrator none; pr none; failure none\n"
            "- verify: blocked; repo erp-service; depends_on implement; "
            "orchestrator none; pr none; failure none\n"
            "- review: blocked; repo erp-service; depends_on verify; "
            "orchestrator none; pr none; failure none"
        ),
    )


async def test_linear_context_comment_replies_with_repo_and_recent_events() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="main",
        metadata={"owner": "platform"},
    )
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(),
            hermes_session=FakeHermesSession(),
            issue_tracker=issue_tracker,
        )
    )

    assignment_response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
                "labels": {"nodes": [{"name": "repo:erp-service"}]},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-context-1"},
    )
    client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-1",
                "body": "Please check inventory allocation first.",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "ENG-1284"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-context-2"},
    )
    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-2",
                "body": "/context ENG-1284",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "ENG-1284"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-context-3"},
    )

    assert assignment_response.status_code == 202
    assert response.status_code == 202
    assert response.json()["task_id"] == assignment_response.json()["task_id"]
    assert issue_tracker.replies[-1] == IssueTrackerReply(
        issue_id="issue-id-1",
        body=(
            "Task ENG-1284 context:\n"
            "Repo: erp-service (github, main)\n"
            "Recent events:\n"
            "- system session_started: Build webhook bridge\n"
            "- linear:user-1 comment: Please check inventory allocation first.\n"
            "- agent reply: I will check inventory allocation first."
        ),
    )


async def test_linear_agents_comment_replies_with_session_and_orchestrator_state() -> None:
    repository = await build_repository()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(),
            hermes_session=FakeHermesSession(),
            issue_tracker=issue_tracker,
        )
    )

    assignment_response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-agents-1"},
    )
    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-1",
                "body": "/agents ENG-1284",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "ENG-1284"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-agents-2"},
    )

    assert assignment_response.status_code == 202
    assert response.status_code == 202
    assert response.json()["task_id"] == assignment_response.json()["task_id"]
    reply = issue_tracker.replies[-1]
    assert reply.issue_id == "issue-id-1"
    assert reply.body.startswith(
        "Task ENG-1284 agents:\n"
        "Orchestrator: multica-task-1 (queued)\n"
        "- linear session "
    )
    assert ": status active, repo none, hermes hermes-session-1, events 2" in reply.body
