from fastapi import APIRouter, Request, status

from agentic_sdlc_platform.glue.dag_decomposer import DagDecomposer
from agentic_sdlc_platform.models.tasks import (
    CreateTaskDagRequest,
    TaskDagNodeResponse,
    TaskDagResponse,
)
from agentic_sdlc_platform.persistence.models import TaskDag

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
