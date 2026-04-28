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


@dataclass(frozen=True)
class IssueAttachment:
    id: str | None = None
    title: str | None = None
    url: str | None = None
    content_type: str | None = None
    content: str | None = None
    metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class IssueComment:
    id: str | None = None
    body: str | None = None
    actor: str | None = None


@dataclass(frozen=True)
class IssueContext:
    issue_id: str
    identifier: str | None = None
    title: str | None = None
    description: str | None = None
    url: str | None = None
    attachments: list[IssueAttachment] | None = None
    comments: list[IssueComment] | None = None


class IssueTrackerPort(Protocol):
    async def create_issue(self, request: IssueCreateRequest) -> IssueCreateResponse:
        raise NotImplementedError

    async def mark_task_queued(self, update: IssueTrackerUpdate) -> None:
        raise NotImplementedError

    async def reply(self, reply: IssueTrackerReply) -> None:
        raise NotImplementedError

    async def get_issue_context(self, issue_id: str) -> IssueContext:
        raise NotImplementedError
