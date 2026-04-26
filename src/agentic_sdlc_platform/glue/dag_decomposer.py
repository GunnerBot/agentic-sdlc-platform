from dataclasses import dataclass, field

from agentic_sdlc_platform.ports.model_provider import ModelProviderPort, ModelRequest


@dataclass(frozen=True)
class Subtask:
    id: str
    title: str
    depends_on: tuple[str, ...] = field(default_factory=tuple)


class DagDecomposer:
    def __init__(self, model_provider: ModelProviderPort | None = None) -> None:
        self._model_provider = model_provider

    async def decompose(self, spec_markdown: str) -> list[Subtask]:
        if not spec_markdown.strip():
            return []

        if self._model_provider is not None:
            await self._model_provider.complete(
                ModelRequest(role="plan_agent", prompt=spec_markdown)
            )

        return [
            Subtask(
                id="scaffold",
                title="Create service scaffold and baseline tests",
            )
        ]
