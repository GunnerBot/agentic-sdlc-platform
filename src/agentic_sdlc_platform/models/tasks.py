from pydantic import BaseModel, Field


class CreateTaskDagRequest(BaseModel):
    spec_markdown: str = Field(min_length=1)
    template: str | None = Field(
        default=None,
        pattern="^(bugfix|feature|refactor|security)$",
    )


class TaskDagNodeResponse(BaseModel):
    node_key: str
    title: str
    repo: str | None = None
    depends_on: list[str]
    status: str
    orchestrator_task_id: str | None = None
    orchestrator_status: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    pr_state: str | None = None
    expected_pr_reference: str | None = None
    expected_branch: str | None = None
    multica_issue_id: str | None = None
    multica_task_id: str | None = None
    multica_agent_id: str | None = None
    multica_runtime_id: str | None = None
    multica_runtime_provider: str | None = None
    failure_error: str | None = None
    retry_count: int = 0
    executions: list["DagNodeExecutionResponse"] = Field(default_factory=list)


class TaskDagResponse(BaseModel):
    id: str
    task_id: str
    status: str
    nodes: list[TaskDagNodeResponse]


class TaskDagSummaryResponse(BaseModel):
    id: str
    status: str
    node_count: int
    ready_count: int
    completed_count: int
    skipped_count: int = 0
    failed_count: int = 0
    first_ready_node: TaskDagNodeResponse | None = None


class CompleteDagNodeResponse(BaseModel):
    completed_node: str
    ready_nodes: list[TaskDagNodeResponse]


class FailDagNodeRequest(BaseModel):
    error: str = Field(min_length=1)


class CreateDagNodeExecutionRequest(BaseModel):
    start: bool = True


class UpdateDagNodeExecutionRequest(BaseModel):
    status: str = Field(
        pattern="^(queued|running|needs_input|pr_open|completed|failed|cancelled)$"
    )
    external_execution_id: str | None = None
    branch_name: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    workspace_path: str | None = None
    error: str | None = None
    metadata: dict[str, object] | None = None


class DagNodeExecutionResponse(BaseModel):
    id: str
    dag_id: str
    node_key: str
    task_id: str
    executor_provider: str
    external_execution_id: str | None = None
    status: str
    branch_name: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    workspace_path: str | None = None
    error: str | None = None
    metadata: dict[str, object]


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
    orchestrator_provider: str | None = None
    orchestrator_issue_id: str | None = None
    orchestrator_task_id: str | None = None
    repo: str | None = None
    status: str
    context_summary: str | None = None
    event_count: int


class AgentSessionDetailResponse(AgentSessionStatusResponse):
    events: list[AgentSessionEventResponse]


class TaskArtifactResponse(BaseModel):
    id: str
    task_id: str
    dag_id: str | None = None
    node_key: str | None = None
    execution_id: str | None = None
    kind: str
    name: str
    content: dict[str, object]
    metadata: dict[str, object]


class TaskStatusResponse(BaseModel):
    id: str
    source: str
    external_id: str
    title: str
    repo: str | None = None
    status: str
    orchestrator_task_id: str | None = None
    orchestrator_status: str | None = None
    dags: list[TaskDagSummaryResponse]
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
    dags: list[TaskDagResponse]
    sessions: list[AgentSessionDetailResponse]
    artifacts: list[TaskArtifactResponse] = Field(default_factory=list)
