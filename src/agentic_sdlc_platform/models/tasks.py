from pydantic import BaseModel, Field


class CreateTaskDagRequest(BaseModel):
    spec_markdown: str = Field(min_length=1)


class TaskDagNodeResponse(BaseModel):
    node_key: str
    title: str
    repo: str | None = None
    depends_on: list[str]
    status: str


class TaskDagResponse(BaseModel):
    id: str
    task_id: str
    status: str
    nodes: list[TaskDagNodeResponse]


class CompleteDagNodeResponse(BaseModel):
    completed_node: str
    ready_nodes: list[TaskDagNodeResponse]
