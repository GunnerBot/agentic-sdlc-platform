from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.glue.execution_policy import (
    PLANNING_ONLY,
    READ_ONLY_QUESTION,
    WRITE_PR,
    bounded_graph_context,
    code_generation_policy,
    github_write_enabled,
    normalize_execution_mode,
)
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
    settings: Settings | None = None,
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
    node_metadata = dict(getattr(node, "metadata_json", {}) or {})
    execution_mode = normalize_execution_mode(
        node_metadata.get("execution_mode"),
        default=_default_execution_mode_for_node(node),
    )
    acceptance_criteria_value = node_metadata.get("acceptance_criteria")
    acceptance_criteria = (
        [
            item
            for item in acceptance_criteria_value
            if isinstance(item, str) and item.strip()
        ]
        if isinstance(acceptance_criteria_value, list)
        else []
    )
    metadata: dict[str, object] = {
        "parent_task_id": task.id,
        "parent_external_id": task.external_id,
        "dag_id": dag.id,
        "node_key": node.node_key,
        "acceptance_criteria": acceptance_criteria,
        "dependency_node_keys": list(node.depends_on),
        "dependencies_completed": completed_dependencies,
        "context_session_id": active_session.id if active_session else None,
        "hermes_session_id": active_session.hermes_session_id if active_session else None,
        "execution_mode": execution_mode,
        "expected_pr_reference": expected_pr_reference(dag.id, node.node_key),
        "expected_branch": expected_branch(dag.id, node.node_key, task.external_id),
        "expected_pr_body_marker": expected_pr_reference(dag.id, node.node_key),
        "code_generation_policy": code_generation_policy(),
        "pr_plan": _pr_plan_metadata(dag=dag, node=node),
    }
    for key in (
        "adversarial_review_required",
        "adversarial_review",
        "adversarial_review_turn_count",
        "adversarial_review_max_turns",
        "latest_adversarial_feedback",
        "revision_requested",
        "latest_quality_feedback",
        "previous_completion_verification",
        "previous_quality_gate",
    ):
        if key in node_metadata:
            metadata[key] = node_metadata[key]
    repo_context = await _repo_context(
        repo=node.repo,
        node_title=_repo_context_question(task=task, node=node, criteria=acceptance_criteria),
        repository=repository,
        graph_store=graph_store,
        settings=settings,
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
    execution_mode = normalize_execution_mode(metadata.get("execution_mode"))
    write_enabled = github_write_enabled(execution_mode)
    branch_name = _str(metadata.get("expected_branch")) if write_enabled else None
    pr_reference = _str(metadata.get("expected_pr_reference")) if write_enabled else None
    execution = await repository.create_dag_node_execution(
        dag_id=dag.id,
        node_key=node.node_key,
        task_id=task.id,
        executor_provider=agent_executor.provider if agent_executor else "none",
        status="queued",
        branch_name=branch_name,
        metadata=metadata,
    )
    existing_inputs = await repository.list_task_artifacts(
        task_id=task.id,
        kind="dag_node_execution_input",
        execution_id=execution.id,
    )
    if not existing_inputs:
        await repository.create_task_artifact(
            task_id=task.id,
            dag_id=dag.id,
            node_key=node.node_key,
            execution_id=execution.id,
            kind="dag_node_execution_input",
            name=f"{node.node_key}:input",
            content={
                "execution_id": execution.id,
                "task_id": task.id,
                "dag_id": dag.id,
                "node_key": node.node_key,
                "title": node.title,
                "repo": node.repo,
                "branch_name": branch_name,
                "pr_reference": pr_reference,
                "metadata": metadata,
            },
            metadata={
                "executor_provider": execution.executor_provider,
                "status": execution.status,
            },
        )
    if agent_executor is None or execution.status == "running" or not write_enabled:
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
                branch_name=branch_name,
                pr_reference=pr_reference,
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


def expected_branch(dag_id: str, node_key: str, external_id: str | None = None) -> str:
    if external_id:
        return f"agent/dag/{_branch_segment(external_id)}/{dag_id}/{node_key}"
    return f"agent/dag/{dag_id}/{node_key}"


def _default_execution_mode_for_node(node) -> str:
    key = str(getattr(node, "node_key", "") or "").lower()
    title = str(getattr(node, "title", "") or "").lower()
    text = f"{key} {title}"
    if any(token in text for token in ("audit", "validate", "validation", "scope", "plan")):
        return PLANNING_ONLY
    if "question" in text or "read only" in text or "read-only" in text:
        return READ_ONLY_QUESTION
    return WRITE_PR


def _branch_segment(value: str) -> str:
    normalized = "".join(
        char.lower() if char.isalnum() else "-"
        for char in value.strip()
    ).strip("-")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return normalized or "task"


def _pr_plan_metadata(*, dag, node) -> dict[str, object]:
    ordered_nodes = list(getattr(dag, "nodes", []) or [])
    ordered_keys = [item.node_key for item in ordered_nodes]
    try:
        pr_index = ordered_keys.index(node.node_key) + 1
    except ValueError:
        pr_index = 1
    dependency_keys = list(getattr(node, "depends_on", []) or [])
    dependent_keys = [
        item.node_key
        for item in ordered_nodes
        if node.node_key in list(getattr(item, "depends_on", []) or [])
    ]
    return {
        "planned_pr_count": len(ordered_nodes) or 1,
        "current_pr_index": pr_index,
        "current_node_key": node.node_key,
        "ordered_node_keys": ordered_keys or [node.node_key],
        "depends_on_prs": dependency_keys,
        "unlocks_prs": dependent_keys,
        "ordering_strategy": "DAG dependency order, then planner order",
        "branch_pattern": "agent/dag/<external_id>/<dag_id>/<node_key>",
        "body_reference_pattern": "dag/<dag_id>/<node_key>",
    }


def _repo_context_question(*, task, node, criteria: list[str]) -> str:
    criteria_text = " ".join(criteria)
    return (
        f"{task.external_id}: {task.title}. DAG node: {node.title}. "
        f"Acceptance criteria: {criteria_text}"
    ).strip()


async def _repo_context(
    *,
    repo: str | None,
    node_title: str,
    repository,
    graph_store: GraphStorePort | None,
    settings: Settings | None = None,
) -> dict[str, object] | None:
    if not repo or graph_store is None:
        return None
    if settings is not None and not settings.vendor_http_enabled:
        return bounded_graph_context(
            status="unavailable",
            reason="graph store access is disabled",
        )
    repo_record = await repository.get_repo_by_name(repo)
    if repo_record is None:
        return bounded_graph_context(
            status="unavailable",
            reason=f"repository {repo} is not registered",
        )
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
        return bounded_graph_context(status="unavailable")
    return bounded_graph_context(
        status="available",
        answer=result.answer,
        references=result.references,
        provider=result.provider,
        max_chars=settings.graphify_context_max_chars if settings else 4000,
        max_references=settings.graphify_context_max_references if settings else 10,
    )


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None
