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


class GraphStorePort(Protocol):
    async def query(self, request: GraphQuery) -> GraphQueryResult:
        """Answer a codebase question using a graph-backed store."""
