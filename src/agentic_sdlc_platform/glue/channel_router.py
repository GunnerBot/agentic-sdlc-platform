import re
from dataclasses import dataclass
from enum import StrEnum


class RouteTarget(StrEnum):
    HERMES_DIRECT = "hermes_direct"
    MULTICA_TASK = "multica_task"
    GRAPH_REPO_QUERY = "graph_repo_query"


@dataclass(frozen=True)
class ChannelMessage:
    channel: str
    text: str
    sender_id: str


class ChannelRouter:
    def route(self, message: ChannelMessage) -> RouteTarget:
        normalized = message.text.strip().lower()
        if normalized.startswith(("/implement", "/ticket", "linear:")):
            return RouteTarget.MULTICA_TASK
        if parse_repo_query(message.text) is not None:
            return RouteTarget.GRAPH_REPO_QUERY
        return RouteTarget.HERMES_DIRECT


@dataclass(frozen=True)
class RepoQuery:
    repo: str
    question: str


def parse_repo_query(text: str) -> RepoQuery | None:
    match = re.match(r"^repo:(?P<repo>[A-Za-z0-9_.\-/]+)\s+(?P<question>.+)$", text.strip())
    if not match:
        return None
    return RepoQuery(
        repo=match.group("repo"),
        question=match.group("question").strip(),
    )
