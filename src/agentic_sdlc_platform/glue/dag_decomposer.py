import json
from dataclasses import dataclass, field

from agentic_sdlc_platform.ports.model_provider import ModelProviderPort, ModelRequest


@dataclass(frozen=True)
class Subtask:
    id: str
    title: str
    repo: str | None = None
    depends_on: tuple[str, ...] = field(default_factory=tuple)


class DagDecomposer:
    def __init__(self, model_provider: ModelProviderPort | None = None) -> None:
        self._model_provider = model_provider

    async def decompose(self, spec_markdown: str) -> list[Subtask]:
        if not spec_markdown.strip():
            return []

        if self._model_provider is not None:
            response = await self._model_provider.complete(
                ModelRequest(role="plan_agent", prompt=spec_markdown)
            )
            parsed = self._parse_subtasks(response.content)
            if parsed:
                return parsed

        return [
            Subtask(
                id="scaffold",
                title="Create service scaffold and baseline tests",
            )
        ]

    def _parse_subtasks(self, content: str) -> list[Subtask]:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []

        subtasks = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            node_id = item.get("id")
            title = item.get("title")
            repo = item.get("repo")
            depends_on = item.get("depends_on", [])
            if not isinstance(node_id, str) or not isinstance(title, str):
                continue
            if repo is not None and not isinstance(repo, str):
                repo = None
            if not isinstance(depends_on, list):
                depends_on = []
            subtasks.append(
                Subtask(
                    id=node_id,
                    title=title,
                    repo=repo,
                    depends_on=tuple(dep for dep in depends_on if isinstance(dep, str)),
                )
            )
        return subtasks
