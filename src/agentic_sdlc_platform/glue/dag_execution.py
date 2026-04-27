from agentic_sdlc_platform.ports.graph_store import GraphQuery, GraphStoreError, GraphStorePort


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
