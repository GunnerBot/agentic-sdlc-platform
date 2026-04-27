from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentic_sdlc_platform.glue.dag_decomposer import Subtask
from agentic_sdlc_platform.persistence.models import Base
from agentic_sdlc_platform.persistence.repository import PersistenceRepository


async def build_repository() -> PersistenceRepository:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return PersistenceRepository(async_sessionmaker(engine, expire_on_commit=False))


async def create_parent_task(repository: PersistenceRepository) -> str:
    event_result = await repository.record_inbound_event(
        source="linear",
        delivery_id="delivery-1",
        event_type="Issue",
        payload={"id": "issue-1"},
    )
    task = await repository.create_task_from_event(
        event_id=event_result.event.id,
        source="linear",
        external_id="OS-1284",
        title="Build agentic SDLC platform",
        repo="keychain-os-erp",
    )
    return task.id


async def test_create_task_dag_persists_nodes_and_dependencies() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)

    dag = await repository.create_task_dag(
        task_id=task_id,
        subtasks=[
            Subtask(id="api", title="Add API contract", repo="erp-api"),
            Subtask(id="web", title="Consume API", repo="erp-web", depends_on=("api",)),
        ],
    )

    assert dag.task_id == task_id
    assert [node.node_key for node in dag.nodes] == ["api", "web"]
    assert dag.nodes[1].depends_on == ("api",)
    assert dag.nodes[1].status == "blocked"


async def test_list_ready_dag_nodes_excludes_blocked_dependencies() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    dag = await repository.create_task_dag(
        task_id=task_id,
        subtasks=[
            Subtask(id="api", title="Add API contract"),
            Subtask(id="web", title="Consume API", depends_on=("api",)),
        ],
    )

    ready = await repository.list_ready_dag_nodes(task_id)
    await repository.mark_dag_node_completed(dag_id=dag.id, node_key="api")
    unblocked = await repository.list_ready_dag_nodes(task_id)

    assert [node.node_key for node in ready] == ["api"]
    assert [node.node_key for node in unblocked] == ["web"]


async def test_update_dag_node_status_and_fetch_dag() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    dag = await repository.create_task_dag(
        task_id=task_id,
        subtasks=[
            Subtask(id="api", title="Add API contract"),
        ],
    )

    await repository.update_dag_node_status(
        dag_id=dag.id,
        node_key="api",
        status="pr_open",
        orchestrator_status="pr_open",
    )
    fetched = await repository.get_task_dag(dag.id)

    assert fetched is not None
    assert fetched.task_id == task_id
    assert fetched.nodes[0].status == "pr_open"
    assert fetched.nodes[0].orchestrator_status == "pr_open"
