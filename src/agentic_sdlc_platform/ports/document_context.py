from dataclasses import dataclass
from typing import Protocol


class DocumentContextError(RuntimeError):
    pass


@dataclass(frozen=True)
class DocumentContext:
    provider: str
    url: str
    title: str | None = None
    text: str | None = None
    metadata: dict[str, object] | None = None


class DocumentContextPort(Protocol):
    async def fetch(self, url: str) -> DocumentContext | None:
        raise NotImplementedError
