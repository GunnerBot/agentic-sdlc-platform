from dataclasses import dataclass
from typing import Protocol


class IssueTrackerError(RuntimeError):
    pass


@dataclass(frozen=True)
class IssueTrackerUpdate:
    issue_id: str
    external_id: str
    internal_task_id: str
    orchestrator_task_id: str | None = None


@dataclass(frozen=True)
class IssueTrackerReply:
    issue_id: str
    body: str


class IssueTrackerPort(Protocol):
    async def mark_task_queued(self, update: IssueTrackerUpdate) -> None:
        raise NotImplementedError

    async def reply(self, reply: IssueTrackerReply) -> None:
        raise NotImplementedError
