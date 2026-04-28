from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status

from agentic_sdlc_platform.glue.conversation_sync import ConversationSyncService
from agentic_sdlc_platform.glue.dag_decomposer import DagDecomposer
from agentic_sdlc_platform.glue.dag_execution import (
    build_dag_node_execution_metadata,
    create_or_start_execution,
)
from agentic_sdlc_platform.glue.dag_templates import build_dag_template
from agentic_sdlc_platform.models.tasks import (
    AgentSessionDetailResponse,
    AgentSessionEventResponse,
    AgentSessionStatusResponse,
    CompleteDagNodeResponse,
    CreateDagNodeExecutionRequest,
    CreateTaskDagRequest,
    DagNodeExecutionResponse,
    FailDagNodeRequest,
    TaskArtifactResponse,
    TaskDagNodeResponse,
    TaskDagResponse,
    TaskDagSummaryResponse,
    TaskDetailResponse,
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
from agentic_sdlc_platform.ports.task_orchestrator import TaskReadRequest, TaskRequest

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
        subtasks = await DagDecomposer(model_provider=request.app.state.model_provider).decompose(
            body.spec_markdown
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
    dag = await _require_task_dag(request, task_id, dag_id)
    node = _node_from_dag(dag, node_key)
    if not node.orchestrator_task_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="DAG node has no orchestrator task",
        )
    read_task = getattr(request.app.state.task_orchestrator, "read_task", None)
    if read_task is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Task orchestrator does not support status sync",
        )
    external_task = await read_task(
        TaskReadRequest(
            external_task_id=node.orchestrator_task_id,
            metadata=dict(node.metadata_json),
        )
    )
    synced_node = await request.app.state.repository.update_dag_node_status(
        dag_id=dag_id,
        node_key=node_key,
        status=_node_status_from_orchestrator(external_task.status),
        orchestrator_status=external_task.status,
        metadata=external_task.metadata,
    )
    return _node_response(synced_node)


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
    )
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
    queued_nodes = []
    for node in ready_nodes:
        metadata = await build_dag_node_execution_metadata(
            dag=dag,
            task=task,
            node=node,
            repository=request.app.state.repository,
            graph_store=request.app.state.graph_store,
        )
        external_task = await request.app.state.task_orchestrator.create_task(
            TaskRequest(
                source="dag",
                external_id=f"{dag.id}:{node.node_key}",
                title=node.title,
                repo=node.repo,
                metadata=metadata,
            )
        )
        persisted_metadata = {
            **metadata,
            **(external_task.metadata or {}),
        }
        queued_nodes.append(
            await request.app.state.repository.mark_dag_node_orchestrated(
                dag_id=dag.id,
                node_key=node.node_key,
                orchestrator_task_id=external_task.external_task_id,
                orchestrator_status=external_task.status,
                metadata=persisted_metadata,
            )
        )
        await create_or_start_execution(
            repository=request.app.state.repository,
            agent_executor=request.app.state.agent_executor,
            dag=dag,
            task=task,
            node=node,
            metadata=persisted_metadata,
        )
    return queued_nodes


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
    return TaskDagNodeResponse(
        node_key=node.node_key,
        title=node.title,
        repo=node.repo,
        depends_on=list(node.depends_on),
        status=status_override or node.status,
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
        if node.status not in {"completed", "skipped", "failed"}
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


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _node_status_from_orchestrator(status: str) -> str:
    return {
        "pending": "queued",
        "queued": "queued",
        "dispatched": "running",
        "running": "running",
        "needs_input": "needs_input",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }.get(status, status)


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
