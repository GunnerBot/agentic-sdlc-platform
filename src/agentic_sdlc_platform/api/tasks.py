from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from agentic_sdlc_platform.glue.dag_decomposer import DagDecomposer
from agentic_sdlc_platform.models.tasks import (
    CompleteDagNodeResponse,
    CreateTaskDagRequest,
    TaskDagNodeResponse,
    TaskDagResponse,
)
from agentic_sdlc_platform.persistence.models import TaskDag
from agentic_sdlc_platform.ports.task_orchestrator import TaskRequest

router = APIRouter(tags=["tasks"])


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
)
async def complete_dag_node(
    task_id: str,
    dag_id: str,
    node_key: str,
    request: Request,
    enqueue_ready: Annotated[str, Query(pattern="^(true|false)$")] = "false",
) -> CompleteDagNodeResponse:
    await request.app.state.repository.mark_dag_node_completed(
        dag_id=dag_id,
        node_key=node_key,
    )
    ready_nodes = await request.app.state.repository.list_ready_dag_nodes(task_id)
    if enqueue_ready == "true" and request.app.state.task_orchestrator is not None:
        ready_nodes = await _enqueue_ready_nodes(
            request=request,
            dag_id=dag_id,
            ready_nodes=ready_nodes,
        )
    return CompleteDagNodeResponse(
        completed_node=node_key,
        ready_nodes=[_node_response(node) for node in ready_nodes],
    )


async def _enqueue_ready_nodes(
    request: Request,
    dag_id: str,
    ready_nodes,
):
    queued_nodes = []
    for node in ready_nodes:
        external_task = await request.app.state.task_orchestrator.create_task(
            TaskRequest(
                source="dag",
                external_id=f"{dag_id}:{node.node_key}",
                title=node.title,
                repo=node.repo,
            )
        )
        queued_nodes.append(
            await request.app.state.repository.mark_dag_node_orchestrated(
                dag_id=dag_id,
                node_key=node.node_key,
                orchestrator_task_id=external_task.external_task_id,
                orchestrator_status=external_task.status,
            )
        )
    return queued_nodes


def _dag_response(dag: TaskDag) -> TaskDagResponse:
    return TaskDagResponse(
        id=dag.id,
        task_id=dag.task_id,
        status=dag.status,
        nodes=[
            TaskDagNodeResponse(
                node_key=node.node_key,
                title=node.title,
                repo=node.repo,
                depends_on=list(node.depends_on),
                status=node.status,
            )
            for node in dag.nodes
        ],
    )


def _node_response(node) -> TaskDagNodeResponse:
    return TaskDagNodeResponse(
        node_key=node.node_key,
        title=node.title,
        repo=node.repo,
        depends_on=list(node.depends_on),
        status=node.status if node.status != "blocked" else "ready",
    )
