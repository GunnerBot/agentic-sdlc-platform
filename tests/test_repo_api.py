from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.persistence.models import Base
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.graph_store import (
    GraphIndexRequest,
    GraphIndexResult,
    GraphQuery,
    GraphQueryResult,
    GraphStoreError,
)
from agentic_sdlc_platform.ports.runtime_repo_registry import (
    RuntimeRepository,
    RuntimeRepoSyncResult,
)
from agentic_sdlc_platform.ports.source_control import SourceInstallation, SourceRepository


class FakeGraphStore:
    def __init__(self) -> None:
        self.index_requests: list[GraphIndexRequest] = []
        self.query_requests: list[GraphQuery] = []

    async def index(self, request: GraphIndexRequest) -> GraphIndexResult:
        self.index_requests.append(request)
        return GraphIndexResult(
            provider="graphify",
            external_index_id=f"idx:{request.repo}",
            status="indexed",
        )

    async def query(self, request: GraphQuery) -> GraphQueryResult:
        self.query_requests.append(request)
        return GraphQueryResult(
            provider="graphify",
            answer="Allocation lives in inventory/allocation.py.",
            references=["inventory/allocation.py"],
        )


class DisabledGraphStore(FakeGraphStore):
    async def index(self, request: GraphIndexRequest) -> GraphIndexResult:
        self.index_requests.append(request)
        raise GraphStoreError("vendor HTTP is disabled")

    async def query(self, request: GraphQuery) -> GraphQueryResult:
        raise GraphStoreError("vendor HTTP is disabled")


class FakeSourceControl:
    provider = "github"

    def __init__(self) -> None:
        self.installation_ids: list[str | None] = []

    async def list_installation_repositories(
        self,
        installation_id: str | None = None,
    ) -> SourceInstallation:
        self.installation_ids.append(installation_id)
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
                    permissions={
                        "pull": True,
                        "push": True,
                        "contents": True,
                        "pull_requests": True,
                    },
                )
            ],
        )


class FakeRuntimeRepoRegistry:
    provider = "multica"

    def __init__(self) -> None:
        self.sync_requests: list[list[RuntimeRepository]] = []

    async def sync_repositories(
        self,
        repositories: list[RuntimeRepository],
    ) -> RuntimeRepoSyncResult:
        self.sync_requests.append(list(repositories))
        return RuntimeRepoSyncResult(
            provider=self.provider,
            workspace_id="multica-workspace-1",
            repo_count=len(repositories),
            urls=tuple(repo.clone_url or repo.name for repo in repositories),
        )


async def build_repository() -> PersistenceRepository:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return PersistenceRepository(async_sessionmaker(engine, expire_on_commit=False))


async def test_create_repo_endpoint_registers_repo_without_runtime_checkout() -> None:
    repository = await build_repository()
    runtime_repo_registry = FakeRuntimeRepoRegistry()
    client = TestClient(
        create_app(
            Settings(),
            repository=repository,
            runtime_repo_registry=runtime_repo_registry,
        )
    )

    response = client.post(
        "/repos",
        json={
            "name": "erp-service",
            "provider": "github",
            "clone_url": "https://github.com/acme-corp/erp-service.git",
            "default_branch": "main",
            "metadata": {"linear_team_key": "OS"},
        },
    )

    assert response.status_code == 201
    assert response.json()["name"] == "erp-service"
    assert response.json()["status"] == "active"
    assert response.json()["metadata"] == {"linear_team_key": "OS"}
    assert runtime_repo_registry.sync_requests == []


async def test_list_and_get_repo_endpoints_return_registered_repos() -> None:
    repository = await build_repository()
    client = TestClient(create_app(Settings(), repository=repository))
    created = client.post(
        "/repos",
        json={
            "name": "erp-service",
            "provider": "github",
            "default_branch": "main",
        },
    )

    list_response = client.get("/repos", params={"provider": "github", "status": "active"})
    get_response = client.get("/repos/erp-service")

    assert created.status_code == 201
    assert list_response.status_code == 200
    assert [repo["name"] for repo in list_response.json()] == ["erp-service"]
    assert get_response.status_code == 200
    assert get_response.json()["id"] == created.json()["id"]


async def test_get_repo_endpoint_returns_404_for_unknown_repo() -> None:
    repository = await build_repository()
    client = TestClient(create_app(Settings(), repository=repository))

    response = client.get("/repos/missing")

    assert response.status_code == 404


async def test_index_repo_endpoint_creates_graphify_index_job() -> None:
    repository = await build_repository()
    graph_store = FakeGraphStore()
    runtime_repo_registry = FakeRuntimeRepoRegistry()
    client = TestClient(
        create_app(
            Settings(),
            repository=repository,
            graph_store=graph_store,
            runtime_repo_registry=runtime_repo_registry,
        )
    )
    client.post(
        "/repos",
        json={
            "name": "erp-service",
            "provider": "github",
            "clone_url": "https://github.com/acme-corp/erp-service.git",
            "default_branch": "main",
            "metadata": {"linear_team_key": "OS"},
        },
    )

    response = client.post("/repos/erp-service/index")
    jobs_response = client.get("/repos/erp-service/index-jobs")

    assert response.status_code == 202
    assert response.json()["status"] == "indexed"
    assert response.json()["external_index_id"] == "idx:erp-service"
    assert jobs_response.status_code == 200
    assert [job["id"] for job in jobs_response.json()] == [response.json()["id"]]
    assert graph_store.index_requests == [
        GraphIndexRequest(
            repo="erp-service",
            clone_url="https://github.com/acme-corp/erp-service.git",
            default_branch="main",
            metadata={"linear_team_key": "OS"},
        )
    ]
    assert runtime_repo_registry.sync_requests == []


async def test_index_selected_repos_endpoint_indexes_only_requested_repos() -> None:
    repository = await build_repository()
    graph_store = FakeGraphStore()
    runtime_repo_registry = FakeRuntimeRepoRegistry()
    client = TestClient(
        create_app(
            Settings(),
            repository=repository,
            graph_store=graph_store,
            runtime_repo_registry=runtime_repo_registry,
        )
    )
    for repo_name in [
        "atlas-tech-inc/keychain-os-erp",
        "atlas-tech-inc/webapp-monorepo",
        "atlas-tech-inc/qa-automation",
    ]:
        local_name = repo_name.split("/", maxsplit=1)[1]
        client.post(
            "/repos",
            json={
                "name": repo_name,
                "provider": "github",
                "clone_url": f"https://github.com/{repo_name}.git",
                "default_branch": "main",
                "metadata": {"local_path": f"/repos/{local_name}"},
            },
        )

    response = client.post(
        "/repos/index",
        json={
            "repos": [
                "atlas-tech-inc/keychain-os-erp",
                "atlas-tech-inc/keychain-os-erp",
                "atlas-tech-inc/webapp-monorepo",
            ]
        },
    )

    assert response.status_code == 202
    assert response.json()["total"] == 2
    assert response.json()["indexed"] == 2
    assert response.json()["failed"] == 0
    assert [job["repo_name"] for job in response.json()["jobs"]] == [
        "atlas-tech-inc/keychain-os-erp",
        "atlas-tech-inc/webapp-monorepo",
    ]
    assert [request.repo for request in graph_store.index_requests] == [
        "atlas-tech-inc/keychain-os-erp",
        "atlas-tech-inc/webapp-monorepo",
    ]
    assert runtime_repo_registry.sync_requests == []


async def test_index_selected_repos_endpoint_rejects_unknown_repos() -> None:
    repository = await build_repository()
    graph_store = FakeGraphStore()
    client = TestClient(create_app(Settings(), repository=repository, graph_store=graph_store))
    client.post(
        "/repos",
        json={
            "name": "atlas-tech-inc/keychain-os-erp",
            "provider": "github",
            "default_branch": "main",
        },
    )

    response = client.post(
        "/repos/index",
        json={
            "repos": [
                "atlas-tech-inc/keychain-os-erp",
                "atlas-tech-inc/missing-repo",
            ]
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == {"missing_repos": ["atlas-tech-inc/missing-repo"]}
    assert graph_store.index_requests == []


async def test_ask_repo_endpoint_queries_graph_store_with_repo_metadata() -> None:
    repository = await build_repository()
    graph_store = FakeGraphStore()
    client = TestClient(create_app(Settings(), repository=repository, graph_store=graph_store))
    client.post(
        "/repos",
        json={
            "name": "erp-service",
            "provider": "github",
            "default_branch": "main",
            "metadata": {"linear_team_key": "OS"},
        },
    )

    response = client.post(
        "/repos/erp-service/ask",
        json={"question": "Where does allocation live?"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "provider": "graphify",
        "answer": "Allocation lives in inventory/allocation.py.",
        "references": ["inventory/allocation.py"],
    }
    assert graph_store.query_requests == [
        GraphQuery(
            repo="erp-service",
            question="Where does allocation live?",
            metadata={"linear_team_key": "OS", "default_branch": "main"},
        )
    ]


async def test_ask_repo_endpoint_returns_404_for_unknown_repo() -> None:
    repository = await build_repository()
    client = TestClient(create_app(Settings(), repository=repository, graph_store=FakeGraphStore()))

    response = client.post("/repos/missing/ask", json={"question": "Where is allocation?"})

    assert response.status_code == 404


async def test_ask_repo_endpoint_returns_503_when_graph_store_is_disabled() -> None:
    repository = await build_repository()
    client = TestClient(
        create_app(Settings(), repository=repository, graph_store=DisabledGraphStore())
    )
    client.post(
        "/repos",
        json={
            "name": "erp-service",
            "provider": "github",
            "default_branch": "main",
        },
    )

    response = client.post(
        "/repos/erp-service/ask",
        json={"question": "Where does allocation live?"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "vendor HTTP is disabled"}


async def test_index_all_repos_indexes_only_active_repositories() -> None:
    repository = await build_repository()
    graph_store = FakeGraphStore()
    runtime_repo_registry = FakeRuntimeRepoRegistry()
    client = TestClient(
        create_app(
            Settings(),
            repository=repository,
            graph_store=graph_store,
            runtime_repo_registry=runtime_repo_registry,
        )
    )
    client.post(
        "/repos",
        json={
            "name": "erp-service",
            "provider": "github",
            "clone_url": "https://github.com/acme-corp/erp-service.git",
            "default_branch": "main",
        },
    )
    client.post(
        "/repos",
        json={
            "name": "legacy-erp",
            "provider": "github",
            "default_branch": "main",
            "status": "disabled",
        },
    )

    response = client.post("/repos/index-all")

    assert response.status_code == 202
    assert [job["repo_name"] for job in response.json()["jobs"]] == ["erp-service"]
    assert response.json()["total"] == 1
    assert response.json()["indexed"] == 1
    assert response.json()["failed"] == 0
    assert graph_store.index_requests == [
        GraphIndexRequest(
            repo="erp-service",
            clone_url="https://github.com/acme-corp/erp-service.git",
            default_branch="main",
            metadata={},
        )
    ]
    assert runtime_repo_registry.sync_requests == []


async def test_index_all_repos_records_failed_jobs_when_graph_store_is_disabled() -> None:
    repository = await build_repository()
    graph_store = DisabledGraphStore()
    client = TestClient(create_app(Settings(), repository=repository, graph_store=graph_store))
    client.post(
        "/repos",
        json={
            "name": "erp-service",
            "provider": "github",
            "default_branch": "main",
        },
    )

    response = client.post("/repos/index-all")

    assert response.status_code == 202
    assert response.json()["total"] == 1
    assert response.json()["indexed"] == 0
    assert response.json()["failed"] == 1
    assert response.json()["jobs"][0]["status"] == "failed"
    assert response.json()["jobs"][0]["error"] == "vendor HTTP is disabled"


async def test_github_app_install_url_endpoint_returns_github_install_screen() -> None:
    client = TestClient(create_app(Settings(github_app_slug="agentic-sdlc")))

    response = client.get(
        "/repos/github-app/install-url",
        params={"workspace_id": "workspace-1"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "workspace_id": "workspace-1",
        "app_slug": "agentic-sdlc",
        "install_url": "https://github.com/apps/agentic-sdlc/installations/new",
        "instructions": (
            "Install the GitHub App, choose the account or organization, and select "
            "the repositories this workspace may read and write."
        ),
    }


async def test_github_app_install_url_endpoint_requires_slug() -> None:
    client = TestClient(create_app(Settings(_env_file=None)))

    response = client.get("/repos/github-app/install-url")

    assert response.status_code == 503
    assert response.json()["detail"] == "GitHub App slug is not configured"


async def test_github_app_installation_endpoint_lists_read_write_repositories() -> None:
    client = TestClient(create_app(Settings(), source_control=FakeSourceControl()))

    response = client.get("/repos/github-app/installation")

    assert response.status_code == 200
    assert response.json() == {
        "provider": "github",
        "installation_id": "installation-1",
        "account": "GunnerBot",
        "repositories": [
            {
                "name": "agentic-sdlc-platform",
                "full_name": "GunnerBot/agentic-sdlc-platform",
                "clone_url": "https://github.com/GunnerBot/agentic-sdlc-platform.git",
                "html_url": "https://github.com/GunnerBot/agentic-sdlc-platform",
                "default_branch": "main",
                "private": True,
                "permissions": {
                    "contents": True,
                    "pull": True,
                    "pull_requests": True,
                    "push": True,
                },
            }
        ],
    }


async def test_github_app_sync_registers_workspace_repositories_with_write() -> None:
    repository = await build_repository()
    source_control = FakeSourceControl()
    runtime_repo_registry = FakeRuntimeRepoRegistry()
    client = TestClient(
        create_app(
            Settings(),
            repository=repository,
            source_control=source_control,
            runtime_repo_registry=runtime_repo_registry,
        )
    )

    response = client.post(
        "/repos/github-app/sync",
        json={"workspace_id": "workspace-1", "installation_id": "installation-2"},
    )

    assert response.status_code == 201
    assert source_control.installation_ids == ["installation-2"]
    assert response.json()["imported"] == 1
    assert response.json()["installation"] == {
        "id": response.json()["installation"]["id"],
        "workspace_id": "workspace-1",
        "provider": "github",
        "installation_id": "installation-2",
        "account": "GunnerBot",
        "repository_selection": "selected",
        "status": "active",
        "permissions": {
            "contents": True,
            "pull": True,
            "pull_requests": True,
            "push": True,
        },
        "metadata": {
            "repo_count": 1,
            "single_app_read_write": True,
        },
    }
    assert response.json()["repositories"][0]["name"] == "GunnerBot/agentic-sdlc-platform"
    assert response.json()["repositories"][0]["metadata"]["workspace_id"] == "workspace-1"
    assert response.json()["repositories"][0]["metadata"]["read_enabled"] is True
    assert response.json()["repositories"][0]["metadata"]["write_enabled"] is True
    assert response.json()["repositories"][0]["metadata"]["allowed_branch_prefix"] == "agent/dag/"
    assert response.json()["repositories"][0]["metadata"]["write_policy"] == {
        "enabled": True,
        "branch_prefix": "agent/dag/",
        "direct_default_branch_push": False,
        "requires_plan_approval": True,
        "auto_merge_enabled": False,
        "requires_pr_body_reference": "dag/<dag_id>/<node_key>",
    }
    assert response.json()["repositories"][0]["metadata"]["github_permissions"] == {
        "contents": True,
        "pull": True,
        "pull_requests": True,
        "push": True,
    }
    assert runtime_repo_registry.sync_requests == []


async def test_github_app_installation_endpoint_requires_configuration() -> None:
    client = TestClient(create_app(Settings(github_app_read_only_enabled=False)))

    response = client.get("/repos/github-app/installation")

    assert response.status_code == 503
