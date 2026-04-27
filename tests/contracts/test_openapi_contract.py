import schemathesis
from hypothesis import HealthCheck, settings

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.model_provider import ModelResponse


class FakeEvent:
    id = "event-1"


class FakeTask:
    id = "task-1"
    source = "linear"
    external_id = "OS-1284"
    title = "Build task status API"
    repo = "keychain-os-erp"
    status = "queued"
    orchestrator_task_id = None
    orchestrator_status = None
    sessions = []


class FakeRepo:
    id = "repo-1"
    name = "keychain-os-erp"
    provider = "github"
    clone_url = None
    default_branch = "main"
    status = "active"
    metadata_json = {}


class FakeWriteResult:
    event = FakeEvent()
    created = True


class FakeDagNode:
    node_key = "api"
    title = "Add API contract"
    repo = "keychain-os-erp"
    depends_on = ()
    status = "ready"


class FakeDag:
    id = "dag-1"
    task_id = "task-1"
    status = "planned"
    nodes = [FakeDagNode()]


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

    async def list_repos(self, **kwargs):
        return [FakeRepo()]

    async def get_repo_by_name(self, name):
        return FakeRepo()

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


class FakeModelProvider:
    async def complete(self, request):
        return ModelResponse(
            provider="fake",
            model="fake-model",
            content='[{"id":"api","title":"Add API contract"}]',
        )


schema = schemathesis.openapi.from_asgi(
    "/openapi.json",
    create_app(
        Settings(),
        repository=FakeRepository(),
        model_provider=FakeModelProvider(),
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
