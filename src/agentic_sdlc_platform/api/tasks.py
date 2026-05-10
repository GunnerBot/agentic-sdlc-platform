from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status

from agentic_sdlc_platform.glue.adversarial_review import normalize_adversarial_review
from agentic_sdlc_platform.glue.conversation_sync import ConversationSyncService
from agentic_sdlc_platform.glue.dag_decomposer import DagDecomposer
from agentic_sdlc_platform.glue.dag_execution import (
    build_dag_node_execution_metadata,
    create_or_start_execution,
)
from agentic_sdlc_platform.glue.dag_orchestrator_sync import (
    DagNodeOrchestratorSyncService,
    node_status_from_orchestrator,
)
from agentic_sdlc_platform.glue.dag_templates import build_dag_template
from agentic_sdlc_platform.glue.execution_policy import (
    DRY_RUN,
    WRITE_PR,
    normalize_execution_mode,
)
from agentic_sdlc_platform.glue.llm_observability import (
    LLM_COST_LEDGER_ARTIFACT_KIND,
    enrich_usage_records,
    record_llm_cost_ledger,
    summarize_usage_records,
    usage_records_from_ledger_artifacts,
    usage_records_from_metadata,
)
from agentic_sdlc_platform.glue.quality_gate import (
    evaluate_completion_quality_gate,
    quality_gate_metadata,
)
from agentic_sdlc_platform.models.tasks import (
    AdversarialReviewResponse,
    AgentSessionDetailResponse,
    AgentSessionEventResponse,
    AgentSessionStatusResponse,
    CompleteDagNodeResponse,
    CreateAdversarialReviewRequest,
    CreateDagNodeExecutionRequest,
    CreateTaskDagRequest,
    DagNodeExecutionResponse,
    FailDagNodeRequest,
    LlmUsageRecordResponse,
    TaskArtifactResponse,
    TaskDagNodeResponse,
    TaskDagResponse,
    TaskDagSummaryResponse,
    TaskDetailResponse,
    TaskLlmObservabilityResponse,
    TaskStatusResponse,
    UpdateDagNodeExecutionRequest,
)
from agentic_sdlc_platform.persistence.models import (
    AgentSession,
    SessionEvent,
    Task,
    TaskArtifact,
    TaskDag,
)
from agentic_sdlc_platform.ports.task_orchestrator import TaskReadRequest

router = APIRouter(tags=["tasks"])


@router.get("", response_model=list[TaskStatusResponse])
async def list_tasks(
    request: Request,
    source: str | None = None,
    repo: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
) -> list[TaskStatusResponse]:
    tasks = await request.app.state.repository.list_tasks(
        source=source,
        repo=repo,
        status=status_filter,
    )
    return [_task_status_response(task) for task in tasks]


@router.get("/{task_id}", response_model=TaskDetailResponse)
async def get_task(task_id: str, request: Request) -> TaskDetailResponse:
    task = await request.app.state.repository.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    return _task_detail_response(task)


@router.get(
    "/{task_id}/artifacts",
    response_model=list[TaskArtifactResponse],
    status_code=status.HTTP_200_OK,
)
async def list_task_artifacts(
    task_id: str,
    request: Request,
    kind: str | None = None,
    dag_id: str | None = None,
    node_key: str | None = None,
    execution_id: str | None = None,
) -> list[TaskArtifactResponse]:
    if await request.app.state.repository.get_task(task_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    artifacts = await request.app.state.repository.list_task_artifacts(
        task_id=task_id,
        kind=kind,
        dag_id=dag_id,
        node_key=node_key,
        execution_id=execution_id,
    )
    return [_artifact_response(artifact) for artifact in artifacts]


@router.get(
    "/{task_id}/llm-observability",
    response_model=TaskLlmObservabilityResponse,
)
async def get_task_llm_observability(
    task_id: str,
    request: Request,
) -> TaskLlmObservabilityResponse:
    task = await request.app.state.repository.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )

    ledger_artifacts = await request.app.state.repository.list_task_artifacts(
        task_id=task.id,
        kind=LLM_COST_LEDGER_ARTIFACT_KIND,
    )
    records: list[dict[str, object]] = usage_records_from_ledger_artifacts(
        ledger_artifacts
    )
    if records:
        summary = summarize_usage_records(records)
        response_records = enrich_usage_records(records)
        return TaskLlmObservabilityResponse(
            task_id=task.id,
            external_id=task.external_id,
            records=[LlmUsageRecordResponse(**record) for record in response_records],
            total_input_tokens=summary["total_input_tokens"],
            total_output_tokens=summary["total_output_tokens"],
            total_tokens=summary["total_tokens"],
            total_estimated_cost_usd=summary["total_estimated_cost_usd"],
            exact_token_record_count=summary["exact_token_record_count"],
            estimated_token_record_count=summary["estimated_token_record_count"],
            provider_cost_record_count=summary["provider_cost_record_count"],
        )

    list_audit_events = getattr(
        request.app.state.repository,
        "list_audit_events_for_targets",
        None,
    )
    audit_events = await list_audit_events([task.id]) if callable(list_audit_events) else []
    for audit_event in audit_events:
        records.extend(
            usage_records_from_metadata(
                dict(audit_event.metadata_json),
                source=f"audit:{audit_event.action}",
                source_id=audit_event.id,
            )
        )
    for session in task.sessions:
        for event in session.events:
            records.extend(
                usage_records_from_metadata(
                    dict(event.metadata_json),
                    source=f"session_event:{event.event_type}",
                    source_id=event.id,
                )
            )
    for dag in task.dags:
        for node in dag.nodes:
            records.extend(
                usage_records_from_metadata(
                    dict(node.metadata_json),
                    source=f"dag_node:{node.node_key}",
                    source_id=getattr(node, "id", None),
                )
            )

    summary = summarize_usage_records(records)
    response_records = enrich_usage_records(records)
    return TaskLlmObservabilityResponse(
        task_id=task.id,
        external_id=task.external_id,
        records=[LlmUsageRecordResponse(**record) for record in response_records],
        total_input_tokens=summary["total_input_tokens"],
        total_output_tokens=summary["total_output_tokens"],
        total_tokens=summary["total_tokens"],
        total_estimated_cost_usd=summary["total_estimated_cost_usd"],
        exact_token_record_count=summary["exact_token_record_count"],
        estimated_token_record_count=summary["estimated_token_record_count"],
        provider_cost_record_count=summary["provider_cost_record_count"],
    )


@router.post(
    "/{task_id}/sessions/{session_id}/sync-orchestrator",
    response_model=AgentSessionDetailResponse,
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"description": "Session not found"}},
)
async def sync_session_orchestrator_comments(
    task_id: str,
    session_id: str,
    request: Request,
) -> AgentSessionDetailResponse:
    task = await request.app.state.repository.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    session = next((item for item in task.sessions if item.id == session_id), None)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    if not session.orchestrator_task_id or not session.orchestrator_issue_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session is not orchestrator-backed",
        )
    try:
        await ConversationSyncService(
            repository=request.app.state.repository,
            task_orchestrator=request.app.state.task_orchestrator,
            issue_tracker=request.app.state.issue_tracker,
            slack_client=request.app.state.slack_client,
            telegram_client=request.app.state.telegram_client,
        ).sync_loaded_session(session)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    read_task = getattr(request.app.state.task_orchestrator, "read_task", None)
    if callable(read_task):
        external_task = await read_task(
            TaskReadRequest(
                external_task_id=session.orchestrator_task_id,
                metadata={
                    "multica_issue_id": session.orchestrator_issue_id,
                    "orchestrator_issue_id": session.orchestrator_issue_id,
                },
            )
        )
        await request.app.state.repository.update_task_status(
            task_id=task.id,
            status=node_status_from_orchestrator(external_task.status),
        )
        await _record_synced_task_usage(
            request=request,
            task_id=task.id,
            dag_id=None,
            node_key=None,
            external_task=external_task,
        )

    refreshed = await request.app.state.repository.get_task(task_id)
    if refreshed is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    refreshed_session = next((item for item in refreshed.sessions if item.id == session_id), None)
    if refreshed_session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    return _session_detail_response(refreshed_session)


@router.post(
    "/{task_id}/dag",
    response_model=TaskDagResponse,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_400_BAD_REQUEST: {"description": "Invalid DAG request"}},
)
async def create_task_dag(
    task_id: str,
    request: Request,
    body: CreateTaskDagRequest,
) -> TaskDagResponse:
    task = await request.app.state.repository.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    if body.template:
        subtasks = build_dag_template(body.template, task)
    else:
        spec_markdown = await _hydrated_spec_markdown(
            request=request,
            task=task,
            spec_markdown=body.spec_markdown,
        )
        subtasks = await DagDecomposer(model_provider=request.app.state.model_provider).decompose(
            spec_markdown
        )
    dag = await request.app.state.repository.create_task_dag(
        task_id=task_id,
        subtasks=subtasks,
    )
    return _dag_response(dag)


@router.post(
    "/{task_id}/dag/{dag_id}/nodes/{node_key}/complete",
    response_model=CompleteDagNodeResponse,
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"description": "DAG not found"}},
)
async def complete_dag_node(
    task_id: str,
    dag_id: str,
    node_key: str,
    request: Request,
    enqueue_ready: Annotated[str, Query(pattern="^(true|false)$")] = "true",
) -> CompleteDagNodeResponse:
    dag = await _require_task_dag(request, task_id, dag_id)
    node = _node_from_dag(dag, node_key)
    quality_gate = evaluate_completion_quality_gate(
        metadata=dict(node.metadata_json),
        expected_pr_reference=f"dag/{dag_id}/{node_key}",
    )
    if not quality_gate.satisfied:
        await request.app.state.repository.update_dag_node_metadata(
            dag_id=dag_id,
            node_key=node_key,
            metadata={"quality_gate": quality_gate_metadata(quality_gate)},
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "DAG node quality gate is not satisfied",
                "missing": list(quality_gate.missing),
            },
        )
    await request.app.state.repository.mark_dag_node_completed(
        dag_id=dag_id,
        node_key=node_key,
    )
    dag = await _require_task_dag(request, task_id, dag_id)
    ready_nodes = await request.app.state.repository.list_ready_dag_nodes_for_dag(dag_id)
    if enqueue_ready == "true" and request.app.state.task_orchestrator is not None:
        ready_nodes = await _enqueue_ready_nodes(
            request=request,
            dag=dag,
            task=dag.task,
            ready_nodes=ready_nodes,
        )
    return CompleteDagNodeResponse(
        completed_node=node_key,
        ready_nodes=[_ready_node_response(node) for node in ready_nodes],
    )


@router.post(
    "/{task_id}/dag/{dag_id}/nodes/{node_key}/fail",
    response_model=TaskDagNodeResponse,
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"description": "DAG not found"}},
)
async def fail_dag_node(
    task_id: str,
    dag_id: str,
    node_key: str,
    request: Request,
    body: FailDagNodeRequest,
) -> TaskDagNodeResponse:
    await _require_task_dag(request, task_id, dag_id)
    node = await request.app.state.repository.mark_dag_node_failed(
        dag_id=dag_id,
        node_key=node_key,
        error=body.error,
    )
    return _node_response(node)


@router.post(
    "/{task_id}/dag/{dag_id}/nodes/{node_key}/skip",
    response_model=CompleteDagNodeResponse,
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"description": "DAG not found"}},
)
async def skip_dag_node(
    task_id: str,
    dag_id: str,
    node_key: str,
    request: Request,
    enqueue_ready: Annotated[str, Query(pattern="^(true|false)$")] = "true",
) -> CompleteDagNodeResponse:
    dag = await _require_task_dag(request, task_id, dag_id)
    await request.app.state.repository.mark_dag_node_skipped(
        dag_id=dag_id,
        node_key=node_key,
    )
    dag = await _require_task_dag(request, task_id, dag_id)
    ready_nodes = await request.app.state.repository.list_ready_dag_nodes_for_dag(dag_id)
    if enqueue_ready == "true" and request.app.state.task_orchestrator is not None:
        ready_nodes = await _enqueue_ready_nodes(
            request=request,
            dag=dag,
            task=dag.task,
            ready_nodes=ready_nodes,
        )
    return CompleteDagNodeResponse(
        completed_node=node_key,
        ready_nodes=[_ready_node_response(node) for node in ready_nodes],
    )


@router.post(
    "/{task_id}/dag/{dag_id}/nodes/{node_key}/retry",
    response_model=TaskDagNodeResponse,
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"description": "DAG not found"}},
)
async def retry_dag_node(
    task_id: str,
    dag_id: str,
    node_key: str,
    request: Request,
    enqueue: Annotated[str, Query(pattern="^(true|false)$")] = "true",
) -> TaskDagNodeResponse:
    dag = await _require_task_dag(request, task_id, dag_id)
    node = await request.app.state.repository.retry_dag_node(
        dag_id=dag_id,
        node_key=node_key,
    )
    dag = await _require_task_dag(request, task_id, dag_id)
    if enqueue == "true" and request.app.state.task_orchestrator is not None:
        queued = await _enqueue_ready_nodes(
            request=request,
            dag=dag,
            task=dag.task,
            ready_nodes=[node],
        )
        node = queued[0] if queued else node
    return _node_response(node)


@router.post(
    "/{task_id}/dag/{dag_id}/nodes/{node_key}/adversarial-reviews",
    response_model=AdversarialReviewResponse,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_404_NOT_FOUND: {"description": "DAG not found"}},
)
async def create_adversarial_review(
    task_id: str,
    dag_id: str,
    node_key: str,
    request: Request,
    body: CreateAdversarialReviewRequest,
) -> AdversarialReviewResponse:
    dag = await _require_task_dag(request, task_id, dag_id)
    node = _node_from_dag(dag, node_key)
    existing_metadata = dict(getattr(node, "metadata_json", {}) or {})
    review_required = (
        body.require_gate or existing_metadata.get("adversarial_review_required") is True
    )
    payload = body.model_dump(exclude_none=True)
    normalized = normalize_adversarial_review(
        payload,
        required=review_required,
    )
    artifact = await request.app.state.repository.create_task_artifact(
        task_id=task_id,
        dag_id=dag_id,
        node_key=node_key,
        kind="adversarial_review",
        name=_adversarial_review_artifact_name(node_key, normalized),
        content=payload,
        metadata=normalized,
    )
    await request.app.state.repository.update_dag_node_metadata(
        dag_id=dag_id,
        node_key=node_key,
        metadata={
            "adversarial_review_required": normalized["required"],
            "adversarial_review": {
                **normalized,
                "artifact_id": artifact.id,
            },
        },
    )
    return _adversarial_review_response(artifact)


@router.get(
    "/{task_id}/dag/{dag_id}/nodes/{node_key}/adversarial-reviews",
    response_model=list[AdversarialReviewResponse],
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"description": "DAG not found"}},
)
async def list_adversarial_reviews(
    task_id: str,
    dag_id: str,
    node_key: str,
    request: Request,
) -> list[AdversarialReviewResponse]:
    await _require_task_dag(request, task_id, dag_id)
    artifacts = await request.app.state.repository.list_task_artifacts(
        task_id=task_id,
        kind="adversarial_review",
        dag_id=dag_id,
        node_key=node_key,
    )
    return [_adversarial_review_response(artifact) for artifact in artifacts]


@router.post(
    "/{task_id}/dag/{dag_id}/nodes/{node_key}/sync-orchestrator",
    response_model=TaskDagNodeResponse,
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"description": "DAG not found"}},
)
async def sync_dag_node_orchestrator_state(
    task_id: str,
    dag_id: str,
    node_key: str,
    request: Request,
) -> TaskDagNodeResponse:
    await _require_task_dag(request, task_id, dag_id)
    read_task = getattr(request.app.state.task_orchestrator, "read_task", None)
    if read_task is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Task orchestrator does not support status sync",
        )
    try:
        await _dag_sync_service(request).sync_node(dag_id=dag_id, node_key=node_key)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    refreshed = await _require_task_dag(request, task_id, dag_id)
    return _node_response(_node_from_dag(refreshed, node_key))


@router.post(
    "/{task_id}/dag/{dag_id}/nodes/{node_key}/executions",
    response_model=DagNodeExecutionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_404_NOT_FOUND: {"description": "DAG not found"}},
)
async def create_dag_node_execution(
    task_id: str,
    dag_id: str,
    node_key: str,
    request: Request,
    body: CreateDagNodeExecutionRequest,
) -> DagNodeExecutionResponse:
    dag = await _require_task_dag(request, task_id, dag_id)
    node = _node_from_dag(dag, node_key)
    metadata = await build_dag_node_execution_metadata(
        dag=dag,
        task=dag.task,
        node=node,
        repository=request.app.state.repository,
        graph_store=request.app.state.graph_store,
        settings=request.app.state.settings,
    )
    execution_mode = normalize_execution_mode(body.execution_mode, default=DRY_RUN)
    if execution_mode == WRITE_PR and not body.confirm_write_pr:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="write_pr execution requires confirm_write_pr=true",
        )
    metadata["execution_mode"] = execution_mode
    execution = await create_or_start_execution(
        repository=request.app.state.repository,
        agent_executor=request.app.state.agent_executor if body.start else None,
        dag=dag,
        task=dag.task,
        node=node,
        metadata=metadata,
    )
    return _execution_response(execution)


@router.get(
    "/{task_id}/dag/{dag_id}/nodes/{node_key}/executions",
    response_model=list[DagNodeExecutionResponse],
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"description": "DAG not found"}},
)
async def list_dag_node_executions(
    task_id: str,
    dag_id: str,
    node_key: str,
    request: Request,
) -> list[DagNodeExecutionResponse]:
    await _require_task_dag(request, task_id, dag_id)
    executions = await request.app.state.repository.list_dag_node_executions(
        dag_id=dag_id,
        node_key=node_key,
    )
    return [_execution_response(execution) for execution in executions]


@router.patch(
    "/{task_id}/dag/{dag_id}/nodes/{node_key}/executions/{execution_id}",
    response_model=DagNodeExecutionResponse,
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"description": "DAG not found"}},
)
async def update_dag_node_execution(
    task_id: str,
    dag_id: str,
    node_key: str,
    execution_id: str,
    request: Request,
    body: UpdateDagNodeExecutionRequest,
) -> DagNodeExecutionResponse:
    await _require_task_dag(request, task_id, dag_id)
    execution = await request.app.state.repository.update_dag_node_execution(
        execution_id=execution_id,
        status=body.status,
        external_execution_id=body.external_execution_id,
        branch_name=body.branch_name,
        pr_url=body.pr_url,
        pr_number=body.pr_number,
        workspace_path=body.workspace_path,
        error=body.error,
        metadata=body.metadata,
    )
    if execution.dag_id != dag_id or execution.node_key != node_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Execution not found",
        )
    return _execution_response(execution)


async def _enqueue_ready_nodes(
    request: Request,
    dag,
    task,
    ready_nodes,
):
    try:
        return await _dag_sync_service(request).enqueue_ready_nodes(
            dag=dag,
            task=task,
            ready_nodes=ready_nodes,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


def _dag_sync_service(request: Request) -> DagNodeOrchestratorSyncService:
    return DagNodeOrchestratorSyncService(
        repository=request.app.state.repository,
        task_orchestrator=request.app.state.task_orchestrator,
        issue_tracker=request.app.state.issue_tracker,
        graph_store=request.app.state.graph_store,
        model_provider=request.app.state.model_provider,
        settings=request.app.state.settings,
        agent_executor=request.app.state.agent_executor,
        runtime_repo_registry=getattr(request.app.state, "runtime_repo_registry", None),
    )


async def _record_synced_task_usage(
    *,
    request: Request,
    task_id: str,
    dag_id: str | None,
    node_key: str | None,
    external_task,
) -> None:
    usage = (external_task.metadata or {}).get("llm_observability")
    if not isinstance(usage, dict):
        return
    source = "task_orchestrator.read_dag_node"
    source_id = external_task.external_task_id
    existing_artifacts = await request.app.state.repository.list_task_artifacts(
        task_id=task_id,
        kind=LLM_COST_LEDGER_ARTIFACT_KIND,
        dag_id=dag_id,
        node_key=node_key,
    )
    for artifact in existing_artifacts:
        content = getattr(artifact, "content_json", None)
        if not isinstance(content, dict):
            continue
        if content.get("source") == source and content.get("source_id") == source_id:
            return
    await record_llm_cost_ledger(
        repository=request.app.state.repository,
        task_id=task_id,
        usage=usage,
        source=source,
        source_id=source_id,
        dag_id=dag_id,
        node_key=node_key,
        metadata={
            "provider": getattr(request.app.state.task_orchestrator, "provider", None),
        },
    )


async def _hydrated_spec_markdown(
    *,
    request: Request,
    task: Task,
    spec_markdown: str,
) -> str:
    issue_tracker = getattr(request.app.state, "issue_tracker", None)
    get_issue_context = getattr(issue_tracker, "get_issue_context", None)
    if task.source != "linear" or not callable(get_issue_context):
        return spec_markdown

    try:
        issue_context = await get_issue_context(task.external_id)
    except Exception as exc:  # pragma: no cover - defensive adapter boundary
        await request.app.state.repository.create_task_artifact(
            task_id=task.id,
            kind="hydrated_spec",
            name=f"{task.external_id}:hydration-failed",
            content={
                "provided_spec_markdown": spec_markdown,
                "error": str(exc),
            },
            metadata={"provider": "linear", "status": "failed"},
        )
        return spec_markdown

    sections = [spec_markdown.strip()]
    if issue_context.title:
        sections.append(f"# Linear title\n{issue_context.title}")
    if issue_context.description:
        sections.append(f"# Linear description\n{issue_context.description}")
    if issue_context.url:
        sections.append(f"# Linear URL\n{issue_context.url}")
    for attachment in issue_context.attachments or []:
        attachment_text = attachment.content or attachment.url
        if attachment_text:
            sections.append(
                f"# Linear attachment: {attachment.title or attachment.id or 'attachment'}\n"
                f"{attachment_text}"
            )
    for comment in issue_context.comments or []:
        if comment.body:
            sections.append(
                f"# Linear comment: {comment.id or comment.actor or 'comment'}\n"
                f"{comment.body}"
            )
    hydrated = "\n\n".join(section for section in sections if section)
    await request.app.state.repository.create_task_artifact(
        task_id=task.id,
        kind="hydrated_spec",
        name=f"{task.external_id}:dag-planning-spec",
        content={
            "provided_spec_markdown": spec_markdown,
            "hydrated_spec_markdown": hydrated,
            "linear": {
                "issue_id": issue_context.issue_id,
                "identifier": issue_context.identifier,
                "title": issue_context.title,
                "url": issue_context.url,
                "attachment_count": len(issue_context.attachments or []),
                "comment_count": len(issue_context.comments or []),
            },
        },
        metadata={"provider": "linear", "status": "hydrated"},
    )
    return hydrated


async def _require_task_dag(request: Request, task_id: str, dag_id: str):
    dag = await request.app.state.repository.get_task_dag(dag_id)
    if dag is None or dag.task_id != task_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="DAG not found",
        )
    return dag


def _node_from_dag(dag, node_key: str):
    node = next((node for node in dag.nodes if node.node_key == node_key), None)
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="DAG node not found",
        )
    return node


def _dag_response(dag: TaskDag) -> TaskDagResponse:
    return TaskDagResponse(
        id=dag.id,
        task_id=dag.task_id,
        status=dag.status,
        nodes=[_node_response(node) for node in dag.nodes],
    )


def _node_response(node, status_override: str | None = None) -> TaskDagNodeResponse:
    metadata = dict(getattr(node, "metadata_json", {}) or {})
    completion_verification = metadata.get("completion_verification")
    completion_verification = (
        completion_verification if isinstance(completion_verification, dict) else {}
    )
    adversarial_review = metadata.get("adversarial_review")
    adversarial_review = (
        adversarial_review if isinstance(adversarial_review, dict) else {}
    )
    return TaskDagNodeResponse(
        node_key=node.node_key,
        title=node.title,
        repo=node.repo,
        depends_on=list(node.depends_on),
        status=status_override or node.status,
        user_status=_str_or_none(metadata.get("user_status")),
        status_reason=_str_or_none(metadata.get("status_reason")),
        status_detail=_str_or_none(metadata.get("status_detail")),
        next_action=_str_or_none(metadata.get("next_action")),
        orchestrator_task_id=node.orchestrator_task_id,
        orchestrator_status=node.orchestrator_status,
        pr_number=_int_or_none(metadata.get("pr_number")),
        pr_url=_str_or_none(metadata.get("pr_url")),
        pr_state=_str_or_none(metadata.get("pr_state")),
        expected_pr_reference=_str_or_none(metadata.get("expected_pr_reference")),
        expected_branch=_str_or_none(metadata.get("expected_branch")),
        multica_issue_id=_str_or_none(metadata.get("multica_issue_id")),
        multica_task_id=_str_or_none(metadata.get("multica_task_id")),
        multica_agent_id=_str_or_none(metadata.get("multica_agent_id")),
        multica_runtime_id=_str_or_none(metadata.get("multica_runtime_id")),
        multica_runtime_provider=_str_or_none(metadata.get("multica_runtime_provider")),
        failure_error=_str_or_none(metadata.get("failure_error")),
        retry_count=_int_or_none(metadata.get("retry_count")) or 0,
        acceptance_criteria=_string_list(metadata.get("acceptance_criteria")),
        verification_status=_str_or_none(completion_verification.get("status")),
        verification_missing=_string_list(completion_verification.get("missing")),
        follow_up_nodes=_string_list(completion_verification.get("follow_up_nodes")),
        adversarial_review_required=metadata.get("adversarial_review_required") is True,
        adversarial_review_status=_str_or_none(adversarial_review.get("status")),
        adversarial_review_score=_float_or_none(adversarial_review.get("score")),
        adversarial_blocking_issue_count=(
            _int_or_none(adversarial_review.get("blocking_issue_count")) or 0
        ),
        executions=[
            _execution_response(execution)
            for execution in node.__dict__.get("executions", [])
        ],
    )


def _ready_node_response(node) -> TaskDagNodeResponse:
    return _node_response(
        node,
        status_override="ready" if node.status == "blocked" else None,
    )


def _task_status_response(task: Task) -> TaskStatusResponse:
    return TaskStatusResponse(
        id=task.id,
        source=task.source,
        external_id=task.external_id,
        title=task.title,
        repo=task.repo,
        status=task.status,
        orchestrator_task_id=task.orchestrator_task_id,
        orchestrator_status=task.orchestrator_status,
        dags=[_dag_summary_response(dag) for dag in getattr(task, "dags", [])],
        sessions=[_session_status_response(session) for session in task.sessions],
    )


def _task_detail_response(task: Task) -> TaskDetailResponse:
    return TaskDetailResponse(
        id=task.id,
        source=task.source,
        external_id=task.external_id,
        title=task.title,
        repo=task.repo,
        status=task.status,
        orchestrator_task_id=task.orchestrator_task_id,
        orchestrator_status=task.orchestrator_status,
        dags=[_dag_response(dag) for dag in getattr(task, "dags", [])],
        sessions=[_session_detail_response(session) for session in task.sessions],
        artifacts=[
            _artifact_response(artifact)
            for artifact in sorted(
                getattr(task, "artifacts", []),
                key=lambda item: (item.created_at, item.id),
                reverse=True,
            )
        ],
    )


def _dag_summary_response(dag: TaskDag) -> TaskDagSummaryResponse:
    completed_nodes = [node for node in dag.nodes if node.status == "completed"]
    skipped_nodes = [node for node in dag.nodes if node.status == "skipped"]
    failed_nodes = [node for node in dag.nodes if node.status == "failed"]
    completed_node_keys = {node.node_key for node in completed_nodes}
    completed_or_skipped_node_keys = completed_node_keys | {
        node.node_key for node in skipped_nodes
    }
    ready_nodes = [
        node
        for node in dag.nodes
        if node.status in {"ready", "blocked"}
        and all(dependency in completed_or_skipped_node_keys for dependency in node.depends_on)
    ]
    return TaskDagSummaryResponse(
        id=dag.id,
        status=dag.status,
        node_count=len(dag.nodes),
        ready_count=len(ready_nodes),
        completed_count=len(completed_nodes),
        skipped_count=len(skipped_nodes),
        failed_count=len(failed_nodes),
        first_ready_node=_ready_node_response(ready_nodes[0]) if ready_nodes else None,
    )


def _session_status_response(session: AgentSession) -> AgentSessionStatusResponse:
    return AgentSessionStatusResponse(
        id=session.id,
        provider=session.provider,
        external_thread_id=session.external_thread_id,
        hermes_session_id=session.hermes_session_id,
        orchestrator_provider=session.orchestrator_provider,
        orchestrator_issue_id=session.orchestrator_issue_id,
        orchestrator_task_id=session.orchestrator_task_id,
        repo=session.repo,
        status=session.status,
        context_summary=session.context_summary,
        event_count=len(session.events),
    )


def _session_detail_response(session: AgentSession) -> AgentSessionDetailResponse:
    status_response = _session_status_response(session)
    return AgentSessionDetailResponse(
        **status_response.model_dump(),
        events=[_session_event_response(event) for event in session.events],
    )


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _execution_response(execution) -> DagNodeExecutionResponse:
    return DagNodeExecutionResponse(
        id=execution.id,
        dag_id=execution.dag_id,
        node_key=execution.node_key,
        task_id=execution.task_id,
        executor_provider=execution.executor_provider,
        external_execution_id=execution.external_execution_id,
        status=execution.status,
        branch_name=execution.branch_name,
        pr_url=execution.pr_url,
        pr_number=execution.pr_number,
        workspace_path=execution.workspace_path,
        error=execution.error,
        metadata=dict(execution.metadata_json),
    )


def _session_event_response(event: SessionEvent) -> AgentSessionEventResponse:
    return AgentSessionEventResponse(
        id=event.id,
        direction=event.direction,
        event_type=event.event_type,
        actor=event.actor,
        message=event.message,
        metadata=event.metadata_json,
    )


def _artifact_response(artifact: TaskArtifact) -> TaskArtifactResponse:
    return TaskArtifactResponse(
        id=artifact.id,
        task_id=artifact.task_id,
        dag_id=artifact.dag_id,
        node_key=artifact.node_key,
        execution_id=artifact.execution_id,
        kind=artifact.kind,
        name=artifact.name,
        content=dict(artifact.content_json),
        metadata=dict(artifact.metadata_json),
    )


def _adversarial_review_artifact_name(
    node_key: str,
    metadata: dict[str, object],
) -> str:
    turn = metadata.get("turn")
    suffix = f"turn-{turn}" if isinstance(turn, int) else "latest"
    return f"{node_key}:adversarial-review:{suffix}"


def _adversarial_review_response(artifact: TaskArtifact) -> AdversarialReviewResponse:
    metadata = dict(artifact.metadata_json)
    return AdversarialReviewResponse(
        id=artifact.id,
        task_id=artifact.task_id,
        dag_id=artifact.dag_id or "",
        node_key=artifact.node_key or "",
        required=metadata.get("required") is True,
        status=_str_or_none(metadata.get("status")) or "unknown",
        phase=_str_or_none(metadata.get("phase")),
        turn=_int_or_none(metadata.get("turn")),
        reviewer=_str_or_none(metadata.get("reviewer")),
        checkpoint_id=_str_or_none(metadata.get("checkpoint_id")),
        score=_float_or_none(metadata.get("score")),
        summary=_str_or_none(metadata.get("summary")),
        approved=metadata.get("approved") is True,
        blocking_issue_count=_int_or_none(metadata.get("blocking_issue_count")) or 0,
        blocking_issues=_dict_list(metadata.get("blocking_issues")),
    )


def _dict_list(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [
        {key: item_value for key, item_value in item.items() if isinstance(item_value, str)}
        for item in value
        if isinstance(item, dict)
    ]
