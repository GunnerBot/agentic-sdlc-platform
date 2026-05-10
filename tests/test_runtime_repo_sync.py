from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentic_sdlc_platform.glue.dag_decomposer import Subtask
from agentic_sdlc_platform.glue.runtime_repo_sync import (
    sync_runtime_repositories_for_execution,
)
from agentic_sdlc_platform.persistence.models import Base
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.runtime_repo_registry import (
    RuntimeRepository,
    RuntimeRepoSyncResult,
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
            workspace_id="workspace-1",
            repo_count=len(repositories),
            urls=tuple(repo.clone_url or repo.name for repo in repositories),
        )


async def build_repository() -> PersistenceRepository:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return PersistenceRepository(async_sessionmaker(engine, expire_on_commit=False))


async def create_task(repository: PersistenceRepository, external_id: str) -> str:
    event_result = await repository.record_inbound_event(
        source="linear",
        delivery_id=f"delivery-{external_id}",
        event_type="Issue",
        payload={"id": external_id},
    )
    task = await repository.create_task_from_event(
        event_id=event_result.event.id,
        source="linear",
        external_id=external_id,
        title=f"Task {external_id}",
        repo=None,
    )
    return task.id


async def test_runtime_repo_sync_keeps_active_parallel_task_repos() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="acme-corp/service-a",
        provider="github",
        clone_url="https://github.com/acme-corp/service-a.git",
        default_branch="main",
        metadata={},
    )
    await repository.upsert_repo(
        name="acme-corp/service-b",
        provider="github",
        clone_url="https://github.com/acme-corp/service-b.git",
        default_branch="main",
        metadata={},
    )
    existing_task_id = await create_task(repository, "ENG-4110")
    existing_dag = await repository.create_task_dag(
        task_id=existing_task_id,
        subtasks=[
            Subtask(
                id="service_a",
                title="Update service A",
                repo="acme-corp/service-a",
            )
        ],
    )
    await repository.mark_dag_node_orchestrated(
        dag_id=existing_dag.id,
        node_key="service_a",
        orchestrator_task_id="multica-service-a",
        orchestrator_status="queued",
        metadata={},
    )
    registry = FakeRuntimeRepoRegistry()

    result = await sync_runtime_repositories_for_execution(
        repository=repository,
        runtime_repo_registry=registry,
        requested_repo="acme-corp/service-b",
    )

    assert result is not None
    assert [repo.name for repo in registry.sync_requests[0]] == [
        "acme-corp/service-b",
        "acme-corp/service-a",
    ]


async def test_runtime_repo_sync_prunes_when_no_nodes_are_active() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="acme-corp/service-a",
        provider="github",
        clone_url="https://github.com/acme-corp/service-a.git",
        default_branch="main",
        metadata={},
    )
    task_id = await create_task(repository, "ENG-4110")
    dag = await repository.create_task_dag(
        task_id=task_id,
        subtasks=[
            Subtask(
                id="service_a",
                title="Update service A",
                repo="acme-corp/service-a",
            )
        ],
    )
    await repository.mark_dag_node_completed(
        dag_id=dag.id,
        node_key="service_a",
        orchestrator_status="completed",
    )
    registry = FakeRuntimeRepoRegistry()

    result = await sync_runtime_repositories_for_execution(
        repository=repository,
        runtime_repo_registry=registry,
        requested_repo=None,
    )

    assert result is not None
    assert registry.sync_requests == [[]]
    assert result.repo_count == 0
