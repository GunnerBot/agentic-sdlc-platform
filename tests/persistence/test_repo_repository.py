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
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="main",
        metadata={"linear_team_key": "OS"},
    )
    updated = await repository.upsert_repo(
        name="erp-service",
        provider="github",
        clone_url="https://github.com/acme-corp/erp-service.git",
        default_branch="develop",
        metadata={"linear_team_key": "ERP"},
    )
    found = await repository.get_repo_by_name("erp-service")

    assert updated.id == repo.id
    assert found is not None
    assert found.default_branch == "develop"
    assert found.metadata_json == {"linear_team_key": "ERP"}


async def test_list_repos_filters_by_provider_and_status() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="erp-service",
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

    assert [repo.name for repo in repos] == ["erp-service"]


async def test_upsert_and_list_github_installations_by_workspace() -> None:
    repository = await build_repository()

    created = await repository.upsert_github_installation(
        workspace_id="workspace-1",
        installation_id="installation-1",
        account="acme-corp",
        repository_selection="selected",
        permissions={"contents": "write", "pull_requests": "write"},
        metadata={"repo_count": 2},
    )
    updated = await repository.upsert_github_installation(
        workspace_id="workspace-1",
        installation_id="installation-1",
        account="acme-corp",
        repository_selection="selected",
        permissions={"contents": "write"},
        metadata={"repo_count": 3},
    )
    await repository.upsert_github_installation(
        workspace_id="workspace-2",
        installation_id="installation-2",
        account="other-org",
        repository_selection="selected",
        permissions={},
        metadata={},
    )

    installations = await repository.list_github_installations(workspace_id="workspace-1")

    assert updated.id == created.id
    assert [installation.installation_id for installation in installations] == [
        "installation-1"
    ]
    assert installations[0].permissions_json == {"contents": "write"}
    assert installations[0].metadata_json == {"repo_count": 3}
