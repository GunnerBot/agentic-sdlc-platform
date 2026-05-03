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
        external_id="ENG-1284",
        title="Build agentic SDLC platform",
        repo="erp-service",
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
        metadata={"pr_number": 17},
    )
    fetched = await repository.get_task_dag(dag.id)

    assert fetched is not None
    assert fetched.task_id == task_id
    assert fetched.nodes[0].status == "pr_open"
    assert fetched.nodes[0].orchestrator_status == "pr_open"
    assert fetched.nodes[0].metadata_json["pr_number"] == 17


async def test_retry_and_skip_dag_node_update_ready_dependencies() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    dag = await repository.create_task_dag(
        task_id=task_id,
        subtasks=[
            Subtask(id="api", title="Add API contract"),
            Subtask(id="web", title="Consume API", depends_on=("api",)),
        ],
    )

    failed = await repository.mark_dag_node_failed(
        dag_id=dag.id,
        node_key="api",
        error="tests failed",
    )
    retried = await repository.retry_dag_node(dag_id=dag.id, node_key="api")
    await repository.mark_dag_node_skipped(dag_id=dag.id, node_key="api")
    ready = await repository.list_ready_dag_nodes_for_dag(dag.id)

    assert failed.status == "failed"
    assert failed.metadata_json["failure_error"] == "tests failed"
    assert retried.status == "ready"
    assert retried.metadata_json["retry_count"] == 1
    assert [node.node_key for node in ready] == ["web"]


async def test_dag_node_execution_records_are_idempotent_while_active() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    dag = await repository.create_task_dag(
        task_id=task_id,
        subtasks=[
            Subtask(id="api", title="Add API contract"),
        ],
    )

    first = await repository.create_dag_node_execution(
        dag_id=dag.id,
        node_key="api",
        task_id=task_id,
        executor_provider="local",
        status="queued",
        branch_name=f"agent/dag/{dag.id}/api",
        metadata={"expected_pr_reference": f"dag/{dag.id}/api"},
    )
    duplicate = await repository.create_dag_node_execution(
        dag_id=dag.id,
        node_key="api",
        task_id=task_id,
        executor_provider="local",
        status="queued",
    )
    updated = await repository.update_dag_node_execution(
        execution_id=first.id,
        status="running",
        external_execution_id="local-1",
        workspace_path="/tmp/workspace",
    )
    listed = await repository.list_dag_node_executions(dag_id=dag.id, node_key="api")

    assert duplicate.id == first.id
    assert updated.status == "running"
    assert updated.external_execution_id == "local-1"
    assert updated.workspace_path == "/tmp/workspace"
    assert [execution.id for execution in listed] == [first.id]
