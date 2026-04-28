from dataclasses import dataclass
from typing import Protocol


class DesignContextError(RuntimeError):
    pass


@dataclass(frozen=True)
class DesignContext:
    provider: str
    url: str
    title: str | None = None
    summary: str | None = None
    metadata: dict[str, object] | None = None


class DesignContextPort(Protocol):
    async def fetch(
        self,
        url: str,
        *,
        title: str | None = None,
        content_type: str | None = None,
    ) -> DesignContext | None:
        raise NotImplementedError
