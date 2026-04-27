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


class AgentSessionEventResponse(BaseModel):
    id: str
    direction: str
    event_type: str
    actor: str
    message: str | None = None
    metadata: dict[str, object]


class AgentSessionStatusResponse(BaseModel):
    id: str
    provider: str
    external_thread_id: str
    hermes_session_id: str | None = None
    repo: str | None = None
    status: str
    context_summary: str | None = None
    event_count: int


class AgentSessionDetailResponse(AgentSessionStatusResponse):
    events: list[AgentSessionEventResponse]


class TaskStatusResponse(BaseModel):
    id: str
    source: str
    external_id: str
    title: str
    repo: str | None = None
    status: str
    orchestrator_task_id: str | None = None
    orchestrator_status: str | None = None
    sessions: list[AgentSessionStatusResponse]


class TaskDetailResponse(BaseModel):
    id: str
    source: str
    external_id: str
    title: str
    repo: str | None = None
    status: str
    orchestrator_task_id: str | None = None
    orchestrator_status: str | None = None
    sessions: list[AgentSessionDetailResponse]
