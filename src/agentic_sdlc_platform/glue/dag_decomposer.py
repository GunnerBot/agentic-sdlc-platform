import json
import re
from dataclasses import dataclass, field

from agentic_sdlc_platform.ports.model_provider import ModelProviderPort, ModelRequest


@dataclass(frozen=True)
class Subtask:
    id: str
    title: str
    repo: str | None = None
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    acceptance_criteria: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, object] = field(default_factory=dict)


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

        return deterministic_subtasks_for_spec(spec_markdown)

    def parse_subtasks(self, content: str) -> list[Subtask]:
        return self._parse_subtasks(content)

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
            acceptance_criteria = item.get("acceptance_criteria", [])
            metadata = item.get("metadata", {})
            if not isinstance(node_id, str) or not isinstance(title, str):
                continue
            if repo is not None and not isinstance(repo, str):
                repo = None
            if not isinstance(depends_on, list):
                depends_on = []
            if not isinstance(acceptance_criteria, list):
                acceptance_criteria = []
            if not isinstance(metadata, dict):
                metadata = {}
            for metadata_key in (
                "reasoning",
                "expected_changes",
                "test_scope",
                "risk_or_dependency",
            ):
                metadata_value = item.get(metadata_key)
                if isinstance(metadata_value, str) and metadata_value.strip():
                    metadata.setdefault(metadata_key, metadata_value)
            subtasks.append(
                Subtask(
                    id=node_id,
                    title=title,
                    repo=repo,
                    depends_on=tuple(dep for dep in depends_on if isinstance(dep, str)),
                    acceptance_criteria=tuple(
                        criterion
                        for criterion in acceptance_criteria
                        if isinstance(criterion, str) and criterion.strip()
                    ),
                    metadata=metadata,
                )
            )
        return subtasks


def deterministic_subtasks_for_spec(spec_markdown: str) -> list[Subtask]:
    text = spec_markdown.lower()
    repo = _repo_from_spec(text)
    backend_repo = _repo_from_spec(text, role="backend") or repo
    frontend_repo = _repo_from_spec(text, role="frontend") or repo
    if _mentions_backend_or_schema(text):
        backend_node_id = "backend_data_model_api"
        subtasks: list[Subtask] = [
            Subtask(
                id=backend_node_id,
                title="Implement backend data model and API change",
                repo=backend_repo,
                acceptance_criteria=_backend_acceptance_criteria(text),
                metadata={
                    "planner": "deterministic",
                    "planning_basis": "backend_or_schema_keywords",
                    "pr_policy": "implementation_and_tests_same_pr",
                },
            ),
        ]
        if _mentions_frontend(text):
            subtasks.append(
                Subtask(
                    id="frontend_workflow",
                    title="Implement frontend workflow",
                    repo=frontend_repo,
                    depends_on=(backend_node_id,),
                    acceptance_criteria=(
                        "Frontend exposes the requested behavior.",
                        "Frontend state and API integration preserve the requested data.",
                        "Frontend tests are included in the same PR.",
                    ),
                    metadata={
                        "planner": "deterministic",
                        "planning_basis": "frontend_keywords",
                        "pr_policy": "implementation_and_tests_same_pr",
                    },
                )
            )
        return subtasks

    if _mentions_frontend(text):
        return [
            Subtask(
                id="backend_contract",
                title="Define and implement backend contract",
                repo=backend_repo,
                acceptance_criteria=(
                    "Backend contract is implemented and documented.",
                    "Backend tests for this contract are included in the same PR.",
                ),
                metadata={
                    "planner": "deterministic",
                    "pr_policy": "implementation_and_tests_same_pr",
                },
            ),
            Subtask(
                id="frontend_implementation",
                title="Implement frontend workflow",
                repo=frontend_repo,
                depends_on=("backend_contract",),
                acceptance_criteria=("Frontend workflow consumes the backend contract.",),
                metadata={
                    "planner": "deterministic",
                    "pr_policy": "implementation_and_tests_same_pr",
                },
            ),
        ]

    return [
        Subtask(
            id="implementation",
            title="Implement requested change",
            repo=repo,
            acceptance_criteria=(
                "Requested behavior is implemented end to end.",
                "Relevant tests or documented verification are included in the same PR.",
            ),
            metadata={
                "planner": "deterministic",
                "pr_policy": "implementation_and_tests_same_pr",
            },
        ),
    ]


def _mentions_backend_or_schema(text: str) -> bool:
    return "be:" in text or _has_any_word(
        text,
        (
            "backend",
            "api",
            "contract",
            "dto",
            "entity",
            "mapper",
            "service",
            "field",
            "column",
            "table",
            "schema",
            "database",
            "db",
            "migration",
            "audit",
            "listing",
            "persistence",
        ),
    )


def _backend_acceptance_criteria(text: str) -> tuple[str, ...]:
    criteria = [
        (
            "Backend contract, domain model, persistence, and service logic "
            "implement the requested behavior."
        ),
        (
            "Create, update, read, and list paths preserve the requested behavior "
            "where applicable."
        ),
        "Relevant automated tests are included in the same PR.",
    ]
    if _mentions_schema(text):
        criteria.insert(
            0,
            (
                "Database or schema migration applies required persistence changes "
                "with the established repo pattern."
            ),
        )
    if "audit" in text:
        criteria.append(
            "Audit storage includes the requested data when the domain uses audit tables."
        )
    if _has_any_word(text, ("listing", "view")):
        criteria.append(
            "Listing views, projections, or queries include the requested data when applicable."
        )
    return tuple(criteria)


def _mentions_schema(text: str) -> bool:
    return _has_any_word(
        text,
        (
            "field",
            "column",
            "table",
            "schema",
            "database",
            "db",
            "migration",
            "audit",
            "persistence",
        ),
    )


def _mentions_frontend(text: str) -> bool:
    return _has_any_word(text, ("frontend", "front-end", "webapp", "ui", "screen"))


def _repo_from_spec(text: str, *, role: str | None = None) -> str | None:
    if role:
        role_pattern = re.compile(
            rf"(?i)\b(?:{re.escape(role)}\s+)?(?:repo|repository)\s*:\s*"
            r"([a-z0-9_.-]+(?:/[a-z0-9_.-]+)?)"
        )
        match = role_pattern.search(text)
        if match:
            return match.group(1).rstrip(".,;")
    generic_pattern = re.compile(
        r"(?i)\b(?:repo|repository)\s*:\s*([a-z0-9_.-]+(?:/[a-z0-9_.-]+)?)"
    )
    match = generic_pattern.search(text)
    if match:
        return match.group(1).rstrip(".,;")
    return None


def _has_any_word(text: str, words: tuple[str, ...]) -> bool:
    pattern = "|".join(re.escape(word) for word in words)
    return bool(re.search(rf"\b(?:{pattern})\b", text))
