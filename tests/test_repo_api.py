from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.persistence.models import Base
from agentic_sdlc_platform.persistence.repository import PersistenceRepository


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
