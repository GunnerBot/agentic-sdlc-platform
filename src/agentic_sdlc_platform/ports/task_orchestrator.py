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
    metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class TaskResponse:
    external_task_id: str
    status: str
    metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class TaskUpdateRequest:
    external_task_id: str
    status: str
    metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class TaskReadRequest:
    external_task_id: str
    metadata: dict[str, object] | None = None


class TaskOrchestratorPort(Protocol):
    provider: str

    async def create_task(self, request: TaskRequest) -> TaskResponse:
        raise NotImplementedError

    async def update_task(self, request: TaskUpdateRequest) -> TaskResponse:
        raise NotImplementedError

    async def read_task(self, request: TaskReadRequest) -> TaskResponse:
        raise NotImplementedError
