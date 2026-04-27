from dataclasses import dataclass, field
from typing import Protocol


class AgentExecutorError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentExecutionRequest:
    execution_id: str
    task_id: str
    dag_id: str
    node_key: str
    title: str
    repo: str | None
    branch_name: str
    pr_reference: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentExecutionResponse:
    external_execution_id: str
    status: str
    branch_name: str | None = None
    workspace_path: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    metadata: dict[str, object] = field(default_factory=dict)


class AgentExecutorPort(Protocol):
    provider: str

    async def start_execution(
        self,
        request: AgentExecutionRequest,
    ) -> AgentExecutionResponse:
        raise NotImplementedError
