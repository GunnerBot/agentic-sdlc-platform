from dataclasses import dataclass, field


@dataclass(frozen=True)
class Subtask:
    id: str
    title: str
    depends_on: tuple[str, ...] = field(default_factory=tuple)


class DagDecomposer:
    async def decompose(self, spec_markdown: str) -> list[Subtask]:
        if not spec_markdown.strip():
            return []

        return [
            Subtask(
                id="scaffold",
                title="Create service scaffold and baseline tests",
            )
        ]
