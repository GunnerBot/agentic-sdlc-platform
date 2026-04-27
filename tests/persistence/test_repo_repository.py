from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentic_sdlc_platform.persistence.models import Base
from agentic_sdlc_platform.persistence.repository import PersistenceRepository


async def build_repository() -> PersistenceRepository:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return PersistenceRepository(async_sessionmaker(engine, expire_on_commit=False))


async def test_upsert_repo_registry_record_and_lookup_by_name() -> None:
    repository = await build_repository()

    repo = await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
        default_branch="main",
        metadata={"linear_team_key": "OS"},
    )
    updated = await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
        default_branch="develop",
        metadata={"linear_team_key": "ERP"},
    )
    found = await repository.get_repo_by_name("keychain-os-erp")

    assert updated.id == repo.id
    assert found is not None
    assert found.default_branch == "develop"
    assert found.metadata_json == {"linear_team_key": "ERP"}


async def test_list_repos_filters_by_provider_and_status() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url=None,
        default_branch="main",
        metadata=None,
    )
    await repository.upsert_repo(
        name="legacy-erp",
        provider="gitlab",
        clone_url=None,
        default_branch="main",
        metadata=None,
        status="disabled",
    )

    repos = await repository.list_repos(provider="github", status="active")

    assert [repo.name for repo in repos] == ["keychain-os-erp"]
