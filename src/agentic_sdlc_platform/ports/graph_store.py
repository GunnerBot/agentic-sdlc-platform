from dataclasses import dataclass, field
from typing import Protocol


class GraphStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class GraphNode:
    id: str
    label: str
    properties: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphEdge:
    source_id: str
    target_id: str
    label: str
    properties: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphQuery:
    repo: str
    question: str
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphQueryResult:
    provider: str
    answer: str
    references: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GraphIndexRequest:
    repo: str
    clone_url: str | None = None
    default_branch: str = "main"
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphIndexResult:
    provider: str
    external_index_id: str
    status: str


class GraphStorePort(Protocol):
    async def index(self, request: GraphIndexRequest) -> GraphIndexResult:
        """Ingest or refresh codebase context for a repository."""

    async def query(self, request: GraphQuery) -> GraphQueryResult:
        """Answer a codebase question using a graph-backed store."""
