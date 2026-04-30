from dataclasses import dataclass, field
from typing import Protocol


class ModelProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelRequest:
    role: str
    prompt: str
    task_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    provider: str
    model: str
    content: str
    request_id: str | None = None
    usage: dict[str, object] | None = None


class ModelProviderPort(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Complete a model request using a configured provider."""
