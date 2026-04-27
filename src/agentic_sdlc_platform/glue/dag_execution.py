from agentic_sdlc_platform.ports.agent_executor import (
    AgentExecutionRequest,
    AgentExecutorError,
    AgentExecutorPort,
)
from agentic_sdlc_platform.ports.graph_store import GraphQuery, GraphStoreError, GraphStorePort

EXECUTOR_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


async def build_dag_node_execution_metadata(
    *,
    dag,
    task,
    node,
    repository,
    graph_store: GraphStorePort | None,
) -> dict[str, object]:
    completed_dependencies = [
        dependency.node_key
        for dependency in dag.nodes
        if dependency.node_key in node.depends_on
        and dependency.status in {"completed", "skipped"}
    ]
    sessions = list(task.__dict__.get("sessions", []))
    active_session = next(
        (session for session in sessions if session.status == "active"),
        None,
    )
    metadata: dict[str, object] = {
        "parent_task_id": task.id,
        "parent_external_id": task.external_id,
        "dag_id": dag.id,
        "node_key": node.node_key,
        "dependency_node_keys": list(node.depends_on),
        "dependencies_completed": completed_dependencies,
        "context_session_id": active_session.id if active_session else None,
        "hermes_session_id": active_session.hermes_session_id if active_session else None,
        "expected_pr_reference": expected_pr_reference(dag.id, node.node_key),
        "expected_branch": expected_branch(dag.id, node.node_key),
        "expected_pr_body_marker": expected_pr_reference(dag.id, node.node_key),
    }
    repo_context = await _repo_context(
        repo=node.repo,
        node_title=node.title,
        repository=repository,
        graph_store=graph_store,
    )
    if repo_context is not None:
        metadata["repo_context"] = repo_context
    return metadata


async def create_or_start_execution(
    *,
    repository,
    agent_executor: AgentExecutorPort | None,
    dag,
    task,
    node,
    metadata: dict[str, object],
) -> object | None:
    branch_name = _str(metadata.get("expected_branch"))
    execution = await repository.create_dag_node_execution(
        dag_id=dag.id,
        node_key=node.node_key,
        task_id=task.id,
        executor_provider=agent_executor.provider if agent_executor else "none",
        status="queued",
        branch_name=branch_name,
        metadata=metadata,
    )
    if agent_executor is None or execution.status == "running":
        return execution

    try:
        response = await agent_executor.start_execution(
            AgentExecutionRequest(
                execution_id=execution.id,
                task_id=task.id,
                dag_id=dag.id,
                node_key=node.node_key,
                title=node.title,
                repo=node.repo,
                branch_name=branch_name or expected_branch(dag.id, node.node_key),
                pr_reference=_str(metadata.get("expected_pr_reference"))
                or expected_pr_reference(dag.id, node.node_key),
                metadata=metadata,
            )
        )
    except AgentExecutorError as exc:
        return await repository.update_dag_node_execution(
            execution_id=execution.id,
            status="failed",
            error=str(exc),
        )

    return await repository.update_dag_node_execution(
        execution_id=execution.id,
        status=response.status,
        external_execution_id=response.external_execution_id,
        branch_name=response.branch_name,
        pr_url=response.pr_url,
        pr_number=response.pr_number,
        workspace_path=response.workspace_path,
        metadata=response.metadata,
    )


def expected_pr_reference(dag_id: str, node_key: str) -> str:
    return f"dag/{dag_id}/{node_key}"


def expected_branch(dag_id: str, node_key: str) -> str:
    return f"agent/dag/{dag_id}/{node_key}"


async def _repo_context(
    *,
    repo: str | None,
    node_title: str,
    repository,
    graph_store: GraphStorePort | None,
) -> dict[str, object] | None:
    if not repo or graph_store is None:
        return None
    repo_record = await repository.get_repo_by_name(repo)
    if repo_record is None:
        return None
    repo_metadata = {
        key: value
        for key, value in dict(repo_record.metadata_json).items()
        if isinstance(key, str) and isinstance(value, str)
    }
    try:
        result = await graph_store.query(
            GraphQuery(
                repo=repo,
                question=f"What code context is relevant for this DAG node: {node_title}?",
                metadata={
                    **repo_metadata,
                    "default_branch": repo_record.default_branch,
                },
            )
        )
    except GraphStoreError:
        return {"status": "unavailable"}
    return {
        "status": "available",
        "answer": result.answer,
        "references": result.references,
        "provider": result.provider,
    }


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None
