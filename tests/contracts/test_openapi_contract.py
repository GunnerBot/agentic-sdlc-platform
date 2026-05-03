import schemathesis
from hypothesis import HealthCheck, settings

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.model_provider import ModelResponse
from agentic_sdlc_platform.ports.source_control import SourceInstallation, SourceRepository


class FakeEvent:
    id = "event-1"


class FakeTask:
    id = "task-1"
    source = "linear"
    external_id = "ENG-1284"
    title = "Build task status API"
    repo = "erp-service"
    status = "queued"
    orchestrator_task_id = None
    orchestrator_status = None
    sessions = []
    dags = []


class FakeRepo:
    id = "repo-1"
    name = "erp-service"
    provider = "github"
    clone_url = None
    default_branch = "main"
    status = "active"
    metadata_json = {}


class FakeRepoIndexJob:
    id = "repo-index-job-1"
    repo_name = "erp-service"
    provider = "graphify"
    external_index_id = "idx:erp-service"
    status = "indexed"
    error = None
    metadata_json = {}


class FakeWriteResult:
    event = FakeEvent()
    created = True


class FakeDagNode:
    node_key = "api"
    title = "Add API contract"
    repo = "erp-service"
    depends_on = ()
    status = "ready"
    orchestrator_task_id = None
    orchestrator_status = None
    metadata_json = {}
    executions = []


class FakeDagNodeExecution:
    id = "execution-1"
    dag_id = "dag-1"
    node_key = "api"
    task_id = "task-1"
    executor_provider = "local"
    external_execution_id = "local:execution-1"
    status = "running"
    branch_name = "agent/dag/dag-1/api"
    pr_url = None
    pr_number = None
    workspace_path = "/tmp/workspace"
    error = None
    metadata_json = {}


class FakeDag:
    id = "dag-1"
    task_id = "task-1"
    status = "planned"
    nodes = [FakeDagNode()]


FakeTask.dags = [FakeDag()]


class FakeRepository:
    async def record_inbound_event(self, **kwargs):
        return FakeWriteResult()

    async def create_task_from_event(self, **kwargs):
        return FakeTask()

    async def find_task_by_external_id(self, external_id):
        return None

    async def update_task_status(self, **kwargs):
        return FakeTask()

    async def list_tasks(self, **kwargs):
        return [FakeTask()]

    async def get_task(self, task_id):
        return FakeTask()

    async def upsert_repo(self, **kwargs):
        return FakeRepo()

    async def upsert_github_installation(self, **kwargs):
        class Installation:
            id = "github-installation-1"
            workspace_id = kwargs.get("workspace_id", "default")
            provider = "github"
            installation_id = kwargs.get("installation_id", "installation-1")
            account = kwargs.get("account")
            repository_selection = kwargs.get("repository_selection", "selected")
            status = kwargs.get("status", "active")
            permissions_json = kwargs.get("permissions", {})
            metadata_json = kwargs.get("metadata", {})

        return Installation()

    async def list_repos(self, **kwargs):
        return [FakeRepo()]

    async def get_repo_by_name(self, name):
        return FakeRepo()

    async def create_repo_index_job(self, **kwargs):
        return FakeRepoIndexJob()

    async def mark_repo_index_job_completed(self, **kwargs):
        return FakeRepoIndexJob()

    async def mark_repo_index_job_failed(self, **kwargs):
        return FakeRepoIndexJob()

    async def list_repo_index_jobs(self, **kwargs):
        return [FakeRepoIndexJob()]

    async def record_audit_event(self, **kwargs):
        return None

    async def create_task_dag(self, **kwargs):
        return FakeDag()

    async def mark_dag_node_completed(self, **kwargs):
        return FakeDagNode()

    async def mark_dag_node_orchestrated(self, **kwargs):
        return FakeDagNode()

    async def list_ready_dag_nodes(self, task_id):
        return [FakeDagNode()]

    async def get_task_dag(self, dag_id):
        return FakeDag()

    async def list_ready_dag_nodes_for_dag(self, dag_id):
        return [FakeDagNode()]

    async def mark_dag_node_failed(self, **kwargs):
        node = FakeDagNode()
        node.status = "failed"
        node.metadata_json = {"failure_error": "failed"}
        return node

    async def mark_dag_node_skipped(self, **kwargs):
        node = FakeDagNode()
        node.status = "skipped"
        return node

    async def retry_dag_node(self, **kwargs):
        node = FakeDagNode()
        node.metadata_json = {"retry_count": 1}
        return node

    async def create_dag_node_execution(self, **kwargs):
        return FakeDagNodeExecution()

    async def update_dag_node_execution(self, **kwargs):
        return FakeDagNodeExecution()

    async def list_dag_node_executions(self, **kwargs):
        return [FakeDagNodeExecution()]

    async def list_active_dag_node_executions(self, **kwargs):
        return [FakeDagNodeExecution()]

    async def create_task_artifact(self, **kwargs):
        return None

    async def list_task_artifacts(self, **kwargs):
        return []


class FakeModelProvider:
    async def complete(self, request):
        return ModelResponse(
            provider="fake",
            model="fake-model",
            content='[{"id":"api","title":"Add API contract"}]',
        )


class FakeGraphStore:
    async def index(self, request):
        class Result:
            external_index_id = "idx:erp-service"
            status = "indexed"

        return Result()

    async def query(self, request):
        class Result:
            provider = "graphify"
            answer = "answer"
            references = []

        return Result()


class FakeSourceControl:
    async def list_installation_repositories(self, installation_id=None):
        return SourceInstallation(
            provider="github",
            installation_id=installation_id or "installation-1",
            account="GunnerBot",
            repositories=[
                SourceRepository(
                    name="agentic-sdlc-platform",
                    full_name="GunnerBot/agentic-sdlc-platform",
                    clone_url="https://github.com/GunnerBot/agentic-sdlc-platform.git",
                    html_url="https://github.com/GunnerBot/agentic-sdlc-platform",
                    default_branch="main",
                    private=True,
                    permissions={"contents": True, "pull_requests": True, "push": True},
                )
            ],
        )


schema = schemathesis.openapi.from_asgi(
    "/openapi.json",
    create_app(
        Settings(github_app_slug="agentic-sdlc"),
        repository=FakeRepository(),
        model_provider=FakeModelProvider(),
        graph_store=FakeGraphStore(),
        source_control=FakeSourceControl(),
    ),
)


@schema.parametrize()
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
)
def test_openapi_contract(case: schemathesis.Case) -> None:
    case.call_and_validate()
