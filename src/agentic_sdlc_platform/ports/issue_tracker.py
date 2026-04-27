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


@dataclass(frozen=True)
class IssueCreateRequest:
    title: str
    description: str
    team_id: str | None = None
    repo: str | None = None
    metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class IssueCreateResponse:
    issue_id: str
    external_id: str
    url: str | None = None


class IssueTrackerPort(Protocol):
    async def create_issue(self, request: IssueCreateRequest) -> IssueCreateResponse:
        raise NotImplementedError

    async def mark_task_queued(self, update: IssueTrackerUpdate) -> None:
        raise NotImplementedError

    async def reply(self, reply: IssueTrackerReply) -> None:
        raise NotImplementedError
