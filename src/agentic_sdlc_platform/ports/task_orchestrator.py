from dataclasses import dataclass
from typing import Protocol


class TaskOrchestratorError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskRequest:
    source: str
    external_id: str
    title: str
    repo: str | None = None
    inbound_event_id: str | None = None


@dataclass(frozen=True)
class TaskResponse:
    external_task_id: str
    status: str


class TaskOrchestratorPort(Protocol):
    async def create_task(self, request: TaskRequest) -> TaskResponse:
        raise NotImplementedError
