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
    async def query(self, request: GraphQuery) -> GraphQueryResult:
        raise GraphStoreError("vendor HTTP is disabled")


async def build_repository() -> PersistenceRepository:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return PersistenceRepository(async_sessionmaker(engine, expire_on_commit=False))


async def test_create_repo_endpoint_registers_repo_for_multi_repo_work() -> None:
    repository = await build_repository()
    client = TestClient(create_app(Settings(), repository=repository))

    response = client.post(
        "/repos",
        json={
            "name": "keychain-os-erp",
            "provider": "github",
            "clone_url": "https://github.com/atlas-tech-inc/keychain-os-erp.git",
            "default_branch": "main",
            "metadata": {"linear_team_key": "OS"},
        },
    )

    assert response.status_code == 201
    assert response.json()["name"] == "keychain-os-erp"
    assert response.json()["status"] == "active"
    assert response.json()["metadata"] == {"linear_team_key": "OS"}


async def test_list_and_get_repo_endpoints_return_registered_repos() -> None:
    repository = await build_repository()
    client = TestClient(create_app(Settings(), repository=repository))
    created = client.post(
        "/repos",
        json={
            "name": "keychain-os-erp",
            "provider": "github",
            "default_branch": "main",
        },
    )

    list_response = client.get("/repos", params={"provider": "github", "status": "active"})
    get_response = client.get("/repos/keychain-os-erp")

    assert created.status_code == 201
    assert list_response.status_code == 200
    assert [repo["name"] for repo in list_response.json()] == ["keychain-os-erp"]
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
    client = TestClient(
        create_app(Settings(), repository=repository, graph_store=graph_store)
    )
    client.post(
        "/repos",
        json={
            "name": "keychain-os-erp",
            "provider": "github",
            "clone_url": "https://github.com/atlas-tech-inc/keychain-os-erp.git",
            "default_branch": "main",
            "metadata": {"linear_team_key": "OS"},
        },
    )

    response = client.post("/repos/keychain-os-erp/index")
    jobs_response = client.get("/repos/keychain-os-erp/index-jobs")

    assert response.status_code == 202
    assert response.json()["status"] == "indexed"
    assert response.json()["external_index_id"] == "idx:keychain-os-erp"
    assert jobs_response.status_code == 200
    assert [job["id"] for job in jobs_response.json()] == [response.json()["id"]]
    assert graph_store.index_requests == [
        GraphIndexRequest(
            repo="keychain-os-erp",
            clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
            default_branch="main",
            metadata={"linear_team_key": "OS"},
        )
    ]


async def test_ask_repo_endpoint_queries_graph_store_with_repo_metadata() -> None:
    repository = await build_repository()
    graph_store = FakeGraphStore()
    client = TestClient(create_app(Settings(), repository=repository, graph_store=graph_store))
    client.post(
        "/repos",
        json={
            "name": "keychain-os-erp",
            "provider": "github",
            "default_branch": "main",
            "metadata": {"linear_team_key": "OS"},
        },
    )

    response = client.post(
        "/repos/keychain-os-erp/ask",
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
            repo="keychain-os-erp",
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
            "name": "keychain-os-erp",
            "provider": "github",
            "default_branch": "main",
        },
    )

    response = client.post(
        "/repos/keychain-os-erp/ask",
        json={"question": "Where does allocation live?"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "vendor HTTP is disabled"}
