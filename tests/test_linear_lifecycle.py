from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.persistence.models import AuditEvent, Base, TaskDag
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.graph_store import GraphQuery, GraphQueryResult
from agentic_sdlc_platform.ports.hermes_session import (
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


class FakePlanningModel:
    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(provider="fake", model="planner-test", content=self.content)


async def build_repository() -> PersistenceRepository:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return PersistenceRepository(async_sessionmaker(engine, expire_on_commit=False))


async def test_linear_assigned_issue_comments_when_agent_task_is_queued() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
        default_branch="main",
        metadata={},
    )
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
                "identifier": "OS-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
                "labels": {"nodes": [{"name": "repo:keychain-os-erp"}]},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-lifecycle-1"},
    )

    assert response.status_code == 202
    assert response.json()["task_id"] is not None
    assert issue_tracker.updates == [
        IssueTrackerUpdate(
            issue_id="issue-id-1",
            external_id="OS-1284",
            internal_task_id=response.json()["task_id"],
            orchestrator_task_id="multica-task-1",
        )
    ]
    assert issue_tracker.replies == [
        IssueTrackerReply(
            issue_id="issue-id-1",
            body=(
                "Accepted OS-1284.\n"
                "Repo: keychain-os-erp.\n"
                "DAG template: none.\n"
                "First DAG node queued: none.\n"
                "Commands: /status OS-1284, /context OS-1284, /agents OS-1284."
            ),
        )
    ]


async def test_linear_assigned_issue_uses_registered_repo_metadata() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
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
                "identifier": "OS-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
                "labels": {"nodes": [{"name": "repo:keychain-os-erp"}]},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-registered-repo-1"},
    )

    assert response.status_code == 202
    assert task_orchestrator.created == [
        TaskRequest(
            source="linear",
            external_id="OS-1284",
            title="Build webhook bridge",
            repo="keychain-os-erp",
            inbound_event_id=task_orchestrator.created[0].inbound_event_id,
            metadata={
                "repo_provider": "github",
                "repo_clone_url": "https://github.com/atlas-tech-inc/keychain-os-erp.git",
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
                "Accepted OS-1284.\n"
                "Repo: keychain-os-erp.\n"
                "DAG template: none.\n"
                "First DAG node queued: none.\n"
                "Commands: /status OS-1284, /context OS-1284, /agents OS-1284."
            ),
        )
    ]


async def test_linear_assigned_issue_includes_graphify_repo_context_when_available() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
        default_branch="develop",
        metadata={"linear_team_key": "OS"},
    )
    graph_store = FakeGraphStore()
    task_orchestrator = FakeTaskOrchestrator()
    client = TestClient(
        create_app(
            Settings(
                linear_agent_user_id="agent-user-1",
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
                "identifier": "OS-1284",
                "title": "Explain foo DAFET validation dry run behaviour",
                "description": "How does it work on form submission?",
                "assignee": {"id": "agent-user-1"},
                "labels": {"nodes": [{"name": "repo:keychain-os-erp"}]},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-graphify-context-1"},
    )

    assert response.status_code == 202
    assert graph_store.queries == [
        GraphQuery(
            repo="keychain-os-erp",
            question=(
                "Explain foo DAFET validation dry run behaviour\n\n"
                "How does it work on form submission?"
            ),
            metadata={"source": "linear", "external_id": "OS-1284"},
        )
    ]
    assert task_orchestrator.created[0].metadata == {
        "repo_provider": "github",
        "repo_clone_url": "https://github.com/atlas-tech-inc/keychain-os-erp.git",
        "repo_default_branch": "develop",
        "repo_metadata": {"linear_team_key": "OS"},
        "repo_context": {
            "status": "available",
            "provider": "graphify",
            "answer": "Foo DAFET dry-run validation is resolved from indexed repo context.",
            "references": ["apps/foo/dafet/form.ts:42"],
        },
    }


async def test_linear_assigned_issue_ingests_multirepo_markdown_spec() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
        default_branch="main",
        metadata={},
    )
    await repository.upsert_repo(
        name="webapp-monorepo",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/webapp-monorepo.git",
        default_branch="main",
        metadata={},
    )
    graph_store = FakeGraphStore()
    task_orchestrator = FakeTaskOrchestrator(
        task_ids=["multica-parent-task-1", "multica-os-node", "multica-web-node"]
    )
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(
                linear_agent_user_id="agent-user-1",
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
                "identifier": "OS-2222",
                "title": "Support dynamic form titles",
                "description": (
                    "## Repositories\n"
                    "- keychain-os-erp\n"
                    "- webapp-monorepo\n\n"
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
        "scope_keychain_os_erp",
        "scope_webapp_monorepo",
    ]
    assert [node.repo for node in dag.nodes] == ["keychain-os-erp", "webapp-monorepo"]
    assert [request.repo for request in task_orchestrator.created] == [
        None,
        "keychain-os-erp",
        "webapp-monorepo",
    ]
    assert task_orchestrator.created[0].metadata["spec_ingestion"]["repo_scope"] == {
        "scope": "multi_repo",
        "repos": [
            {"repo": "keychain-os-erp", "reason": "mentioned_in_spec"},
            {"repo": "webapp-monorepo", "reason": "mentioned_in_spec"},
        ],
        "unknown_repos": [],
    }
    assert task_orchestrator.created[1].metadata["spec_ingestion"]["repo_scope"][
        "scope"
    ] == "multi_repo"
    assert task_orchestrator.created[2].metadata["spec_ingestion"]["repo_scope"][
        "scope"
    ] == "multi_repo"
    assert [query.repo for query in graph_store.queries] == [
        "keychain-os-erp",
        "webapp-monorepo",
    ]
    assert spec_event.metadata_json["repo_scope"]["scope"] == "multi_repo"
    assert issue_tracker.replies == [
        IssueTrackerReply(
            issue_id="issue-id-1",
            body=(
                "Accepted OS-2222.\n"
                "Repo: none.\n"
                "Spec repo scope: multi_repo (keychain-os-erp, webapp-monorepo).\n"
                "DAG template: linear-spec.\n"
                "Planned nodes: 2 (scope_keychain_os_erp, scope_webapp_monorepo).\n"
                "First DAG node queued: scope_keychain_os_erp (queued).\n"
                "Commands: /status OS-2222, /context OS-2222, /agents OS-2222."
            ),
        )
    ]


async def test_linear_assigned_issue_uses_model_planner_for_spec_dag() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
        default_branch="main",
        metadata={},
    )
    await repository.upsert_repo(
        name="webapp-monorepo",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/webapp-monorepo.git",
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
    "repo": "keychain-os-erp"
  },
  {
    "id": "backend_impl",
    "title": "Persist and expose dynamic form title metadata",
    "repo": "keychain-os-erp",
    "depends_on": ["backend_contract"]
  },
  {
    "id": "frontend_impl",
    "title": "Render dynamic form titles in the web app",
    "repo": "webapp-monorepo",
    "depends_on": ["backend_contract"]
  }
]
"""
    )
    task_orchestrator = FakeTaskOrchestrator(
        task_ids=["multica-parent-task-1", "multica-contract-node"]
    )
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(
                linear_agent_user_id="agent-user-1",
                linear_spec_planner_enabled=True,
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
                "identifier": "OS-2444",
                "title": "Support dynamic form titles",
                "description": (
                    "## Repositories\n"
                    "- keychain-os-erp\n"
                    "- webapp-monorepo\n\n"
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
        "keychain-os-erp",
        "keychain-os-erp",
        "webapp-monorepo",
    ]
    assert dag.nodes[0].status == "queued"
    assert dag.nodes[1].status == "blocked"
    assert dag.nodes[2].status == "blocked"
    assert task_orchestrator.created[1].external_id == f"{dag.id}:backend_contract"
    assert task_orchestrator.created[1].metadata["planning_strategy"] == "model"
    assert planned_event.metadata_json["strategy"] == "model"
    assert planned_event.metadata_json["node_count"] == 3
    assert [request.role for request in planning_model.requests] == ["plan_agent"]
    assert "keychain-os-erp" in planning_model.requests[0].prompt
    assert "webapp-monorepo" in planning_model.requests[0].prompt
    assert [query.repo for query in graph_store.queries[:2]] == [
        "keychain-os-erp",
        "webapp-monorepo",
    ]
    assert issue_tracker.replies == [
        IssueTrackerReply(
            issue_id="issue-id-1",
            body=(
                "Accepted OS-2444.\n"
                "Repo: none.\n"
                "Spec repo scope: multi_repo (keychain-os-erp, webapp-monorepo).\n"
                "DAG template: linear-spec.\n"
                "Planned nodes: 3 (backend_contract, backend_impl, frontend_impl).\n"
                "First DAG node queued: backend_contract (queued).\n"
                "Commands: /status OS-2444, /context OS-2444, /agents OS-2444."
            ),
        )
    ]


async def test_linear_model_planner_falls_back_when_plan_references_unknown_repo() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
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
    task_orchestrator = FakeTaskOrchestrator(
        task_ids=["multica-parent-task-1", "multica-fallback-node"]
    )
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(
                linear_agent_user_id="agent-user-1",
                linear_spec_planner_enabled=True,
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
                "identifier": "OS-2445",
                "title": "Support dynamic form titles",
                "description": (
                    "## Repositories\n"
                    "- keychain-os-erp\n\n"
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

    assert [node.node_key for node in dag.nodes] == ["scope_keychain_os_erp"]
    assert [node.repo for node in dag.nodes] == ["keychain-os-erp"]
    assert planned_event.metadata_json["strategy"] == "repo_fallback"
    assert planned_event.metadata_json["fallback_reason"] == "invalid_model_plan"
    assert task_orchestrator.created[1].metadata["planning_strategy"] == "repo_fallback"


async def test_linear_spec_plan_waits_for_approval_before_node_execution() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
        default_branch="main",
        metadata={},
    )
    planning_model = FakePlanningModel(
        """
[
  {
    "id": "backend_contract",
    "title": "Add dynamic title contract",
    "repo": "keychain-os-erp"
  }
]
"""
    )
    task_orchestrator = FakeTaskOrchestrator(
        task_ids=["multica-parent-task-1", "multica-contract-node"]
    )
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
                "identifier": "OS-2446",
                "title": "Support dynamic form titles",
                "description": (
                    "## Repositories\n"
                    "- keychain-os-erp\n\n"
                    "## Acceptance\n"
                    "- Backend supplies title metadata."
                ),
                "assignee": {"id": "agent-user-1"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-plan-approval-1"},
    )

    task = await repository.find_task_by_external_id("OS-2446")
    assert assignment_response.status_code == 202
    assert task is not None
    assert task.status == "needs_plan_approval"
    assert [request.external_id for request in task_orchestrator.created] == ["OS-2446"]
    assert issue_tracker.replies[-1] == IssueTrackerReply(
        issue_id="issue-id-1",
        body=(
            "Accepted OS-2446.\n"
            "Repo: keychain-os-erp.\n"
            "Spec repo scope: single_repo (keychain-os-erp).\n"
            "DAG template: linear-spec.\n"
            "Planned nodes: 1 (backend_contract).\n"
            "Plan approval required: reply /approve-plan OS-2446 to start.\n"
            "First DAG node queued: none.\n"
            "Commands: /status OS-2446, /context OS-2446, /agents OS-2446."
        ),
    )

    approval_response = client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-1",
                "body": "/approve-plan OS-2446",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "OS-2446"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-plan-approval-2"},
    )

    assert approval_response.status_code == 202
    task = await repository.find_task_by_external_id("OS-2446")
    assert task is not None
    assert task.status == "queued"
    assert [request.external_id for request in task_orchestrator.created] == [
        "OS-2446",
        f"{task.dags[0].id}:backend_contract",
    ]
    assert task_orchestrator.created[1].metadata["plan_approved"] is True
    assert task_orchestrator.created[1].metadata["execution_policy"] == {
        "terminal_command_prefix": "rtk",
        "repo_context_policy": "graphstore_first_then_narrow_source_verification",
        "github_write_enabled": False,
    }
    assert issue_tracker.replies[-1] == IssueTrackerReply(
        issue_id="issue-id-1",
        body="Plan approved for OS-2446. Queued nodes: backend_contract.",
    )


async def test_linear_assigned_issue_ingests_design_assets_for_hermes_context() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="webapp-monorepo",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/webapp-monorepo.git",
        default_branch="main",
        metadata={},
    )
    hermes_session = FakeHermesSession()
    task_orchestrator = FakeTaskOrchestrator()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
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
                    "- webapp-monorepo\n\n"
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
                            "content": "## Repositories\n- webapp-monorepo",
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
            "- webapp-monorepo\n\n"
            "## Design\n"
            "Use https://www.figma.com/file/abc123/form-title-flow\n\n"
            "Ingested Linear spec context:\n"
            "Repos: webapp-monorepo\n"
            "Design assets:\n"
            "- form-title-mobile.png (https://linear.local/form-title-mobile.png)\n"
            "- Figma link (https://www.figma.com/file/abc123/form-title-flow)"
        ),
        repo="webapp-monorepo",
    )
    assert task_orchestrator.created[0].metadata["spec_ingestion"]["asset_count"] == 2
    assert issue_tracker.replies == [
        IssueTrackerReply(
            issue_id="issue-id-1",
            body=(
                "Accepted WEB-991.\n"
                "Repo: webapp-monorepo.\n"
                "Spec repo scope: single_repo (webapp-monorepo).\n"
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
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
        default_branch="main",
        metadata={},
    )
    await repository.upsert_repo(
        name="webapp-monorepo",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/webapp-monorepo.git",
        default_branch="main",
        metadata={},
    )
    task_orchestrator = FakeTaskOrchestrator(
        task_ids=["multica-parent-task-1", "multica-os-node", "multica-web-node"]
    )
    issue_tracker = FakeIssueTracker(
        hydrated_issues={
            "issue-id-1": IssueContext(
                issue_id="issue-id-1",
                identifier="OS-3001",
                title="Support dynamic form titles",
                description=(
                    "## Repositories\n"
                    "- keychain-os-erp\n"
                    "- webapp-monorepo\n\n"
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
                "identifier": "OS-3001",
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

    assert [node.repo for node in dag.nodes] == ["keychain-os-erp", "webapp-monorepo"]
    assert task_orchestrator.created[0].metadata["spec_ingestion"]["asset_count"] == 1
    assert spec_event.metadata_json["text_sources"][0]["title"] == "Linear description"
    assert issue_tracker.replies[-1] == IssueTrackerReply(
        issue_id="issue-id-1",
        body=(
            "Accepted OS-3001.\n"
            "Repo: none.\n"
            "Spec repo scope: multi_repo (keychain-os-erp, webapp-monorepo).\n"
            "Design assets: 1.\n"
            "DAG template: linear-spec.\n"
            "Planned nodes: 2 (scope_keychain_os_erp, scope_webapp_monorepo).\n"
            "First DAG node queued: scope_keychain_os_erp (queued).\n"
            "Commands: /status OS-3001, /context OS-3001, /agents OS-3001."
        ),
    )


async def test_linear_assigned_issue_blocks_and_asks_for_repo_when_spec_is_ambiguous() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
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
                "identifier": "OS-3002",
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
    task = await repository.find_task_by_external_id("OS-3002")
    assert task is not None
    assert task.status == "blocked"
    assert issue_tracker.updates == []
    assert issue_tracker.replies == [
        IssueTrackerReply(
            issue_id="issue-id-1",
            body=(
                "I need a registered repository before I can start OS-3002.\n"
                "Mention one of: keychain-os-erp.\n"
                "Unregistered repo mentions: missing-service."
            ),
        )
    ]


async def test_linear_repo_clarification_comment_resumes_blocked_task() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
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
                "identifier": "OS-3003",
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
                "body": "Use keychain-os-erp for this.",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "OS-3003"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-clarification-resume-2"},
    )

    assert assignment_response.status_code == 202
    assert response.status_code == 202
    assert response.json()["task_id"] == assignment_response.json()["task_id"]
    task = await repository.find_task_by_external_id("OS-3003")
    assert task is not None
    assert task.repo == "keychain-os-erp"
    assert task.status == "queued"
    assert task_orchestrator.created == [
        TaskRequest(
            source="linear",
            external_id="OS-3003",
            title="Support dynamic form titles",
            repo="keychain-os-erp",
            metadata={
                "repo_provider": "github",
                "repo_clone_url": "https://github.com/atlas-tech-inc/keychain-os-erp.git",
                "repo_default_branch": "main",
                "repo_metadata": {},
                "repo_context": {
                    "status": "unavailable",
                    "reason": "graphify CLI query requires graph_path or repo local_path metadata",
                },
                "repo_clarification": {
                    "comment_id": "comment-1",
                    "actor": "linear:user-1",
                    "resolved_repo": "keychain-os-erp",
                },
            },
        )
    ]
    assert issue_tracker.updates == [
        IssueTrackerUpdate(
            issue_id="issue-id-1",
            external_id="OS-3003",
            internal_task_id=response.json()["task_id"],
            orchestrator_task_id="multica-task-1",
        )
    ]
    assert issue_tracker.replies[-1] == IssueTrackerReply(
        issue_id="issue-id-1",
        body="Thanks, I will use keychain-os-erp and start OS-3003.",
    )


async def test_linear_assigned_issue_with_type_label_creates_dag_template() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
        default_branch="main",
        metadata={},
    )
    task_orchestrator = FakeTaskOrchestrator(
        task_ids=["multica-parent-task-1", "multica-dag-node-1"]
    )
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
                "identifier": "OS-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
                "labels": {
                    "nodes": [
                        {"name": "repo:keychain-os-erp"},
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
        assert [node.repo for node in dag.nodes] == ["keychain-os-erp"] * 4
        assert dag.nodes[0].status == "queued"
        assert dag.nodes[0].orchestrator_task_id == "multica-dag-node-1"
        assert dag.nodes[1].status == "blocked"
    assert task_orchestrator.created[0] == TaskRequest(
        source="linear",
        external_id="OS-1284",
        title="Build webhook bridge",
        repo="keychain-os-erp",
        inbound_event_id=task_orchestrator.created[0].inbound_event_id,
        metadata={
            "repo_provider": "github",
            "repo_clone_url": "https://github.com/atlas-tech-inc/keychain-os-erp.git",
            "repo_default_branch": "main",
            "repo_metadata": {},
            "repo_context": {
                "status": "unavailable",
                "reason": "graphify CLI query requires graph_path or repo local_path metadata",
            },
        },
    )
    assert task_orchestrator.created[1].source == "dag"
    assert task_orchestrator.created[1].external_id == f"{dag.id}:reproduce"
    assert task_orchestrator.created[1].metadata == {
        "parent_task_id": response.json()["task_id"],
        "parent_external_id": "OS-1284",
        "dag_id": dag.id,
        "node_key": "reproduce",
        "dependency_node_keys": [],
        "dependencies_completed": [],
        "context_session_id": None,
        "hermes_session_id": None,
        "expected_pr_reference": f"dag/{dag.id}/reproduce",
        "expected_branch": f"agent/dag/{dag.id}/reproduce",
        "expected_pr_body_marker": f"dag/{dag.id}/reproduce",
        "repo_context": {"status": "unavailable"},
    }
    assert dag_created_event.metadata_json["template"] == "bugfix"
    assert dag_created_event.metadata_json["node_count"] == 4
    assert dag_node_enqueued_event.metadata_json["node_key"] == "reproduce"
    assert dag_node_enqueued_event.metadata_json["orchestrator_task_id"] == "multica-dag-node-1"
    assert issue_tracker.replies == [
        IssueTrackerReply(
            issue_id="issue-id-1",
            body=(
                "Accepted OS-1284.\n"
                "Repo: keychain-os-erp.\n"
                "DAG template: bugfix.\n"
                "First DAG node queued: reproduce (queued).\n"
                "Commands: /status OS-1284, /context OS-1284, /agents OS-1284."
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
                "identifier": "OS-1284",
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
                "Register it before I can work on OS-1284."
            ),
        )
    ]
    task = await repository.find_task_by_external_id("OS-1284")
    assert task is not None
    assert task.status == "blocked"


async def test_linear_assigned_issue_starts_and_persists_hermes_session() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
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
                "identifier": "OS-1284",
                "title": "Build webhook bridge",
                "description": "Create the bridge.",
                "assignee": {"id": "agent-user-1"},
                "labels": {"nodes": [{"name": "repo:keychain-os-erp"}]},
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
            repo="keychain-os-erp",
        )
    ]
    persisted = await repository.find_agent_session(
        provider="linear",
        external_thread_id="issue-id-1",
    )
    assert persisted is not None
    assert persisted.hermes_session_id == "hermes-session-1"


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
                "identifier": "OS-1284",
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
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
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
                "identifier": "OS-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
                "labels": {"nodes": [{"name": "repo:keychain-os-erp"}]},
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
                "issue": {"id": "issue-id-1", "identifier": "OS-1284"},
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
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
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
                "identifier": "OS-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
                "labels": {"nodes": [{"name": "repo:keychain-os-erp"}]},
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
                "issue": {"id": "issue-id-1", "identifier": "OS-1284"},
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
                "identifier": "OS-1284",
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
                "issue": {"id": "issue-id-1", "identifier": "OS-1284"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-loop-2"},
    )

    assert assignment_response.status_code == 202
    assert response.status_code == 202
    assert response.json()["task_id"] == assignment_response.json()["task_id"]
    assert hermes_session.resumed == []
    assert len(issue_tracker.replies) == 1
    assert issue_tracker.replies[0].body.startswith("Accepted OS-1284.")


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
                "identifier": "OS-1284",
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
                "body": "/pause OS-1284 waiting for product decision",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "OS-1284"},
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
        body="Command /pause applied. Task OS-1284 is now paused.",
    )
    task = await repository.find_task_by_external_id("OS-1284")
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
                "identifier": "OS-1284",
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
                "body": "/status OS-1284",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "OS-1284"},
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
            "Task OS-1284 status: queued. "
            "Orchestrator: multica-task-1 (queued). "
            "Repo: none. Sessions: 1 active session. "
            "DAG: none."
        ),
    )


async def test_linear_status_comment_replies_with_dag_progress() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
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
                "identifier": "OS-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
                "labels": {
                    "nodes": [
                        {"name": "repo:keychain-os-erp"},
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
                "body": "/status OS-1284",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "OS-1284"},
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
                "Task OS-1284 status: queued. "
                "Orchestrator: multica-task-1 (queued). "
                "Repo: keychain-os-erp. Sessions: 1 active session. "
                "DAG: planned, 0/5 completed, 0 skipped, 0 failed, 1 ready, next: design."
            ),
        )


async def test_linear_nodes_comment_replies_with_dag_node_statuses() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
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
                "identifier": "OS-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
                "labels": {
                    "nodes": [
                        {"name": "repo:keychain-os-erp"},
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
                "body": "/nodes OS-1284",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "OS-1284"},
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
                "Task OS-1284 nodes:\n"
                "Next runnable: design\n"
                "- design: queued; repo keychain-os-erp; depends_on none; "
                "orchestrator multica-task-2; pr none; failure none\n"
                "- contract: blocked; repo keychain-os-erp; depends_on design; "
                "orchestrator none; pr none; failure none\n"
                "- implement: blocked; repo keychain-os-erp; depends_on contract; "
                "orchestrator none; pr none; failure none\n"
                "- verify: blocked; repo keychain-os-erp; depends_on implement; "
                "orchestrator none; pr none; failure none\n"
                "- review: blocked; repo keychain-os-erp; depends_on verify; "
                "orchestrator none; pr none; failure none"
            ),
        )


async def test_linear_context_comment_replies_with_repo_and_recent_events() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
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
                "identifier": "OS-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
                "labels": {"nodes": [{"name": "repo:keychain-os-erp"}]},
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
                "issue": {"id": "issue-id-1", "identifier": "OS-1284"},
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
                "body": "/context OS-1284",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "OS-1284"},
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
            "Task OS-1284 context:\n"
            "Repo: keychain-os-erp (github, main)\n"
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
                "identifier": "OS-1284",
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
                "body": "/agents OS-1284",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "OS-1284"},
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
        "Task OS-1284 agents:\n"
        "Orchestrator: multica-task-1 (queued)\n"
        "- linear session "
    )
    assert ": status active, repo none, hermes hermes-session-1, events 2" in reply.body
