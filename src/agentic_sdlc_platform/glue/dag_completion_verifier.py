import re
from dataclasses import dataclass, field

from agentic_sdlc_platform.glue.dag_decomposer import Subtask

FOLLOWUP_SECTION_RE = re.compile(
    r"(?is)(?:next steps?|follow[- ]?ups?|remaining work|not included|left .*? to .*?:)(.*)"
)
PR_URL_RE = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/\d+")


@dataclass(frozen=True)
class CompletionVerification:
    status: str
    missing: tuple[str, ...] = field(default_factory=tuple)
    followups: tuple[Subtask, ...] = field(default_factory=tuple)
    evidence: dict[str, object] = field(default_factory=dict)

    @property
    def satisfied(self) -> bool:
        return self.status == "satisfied"


def verify_node_completion(
    *,
    node_key: str,
    node_title: str,
    repo: str | None,
    metadata: dict[str, object],
    external_metadata: dict[str, object],
) -> CompletionVerification:
    output = _result_output(external_metadata)
    acceptance_criteria = _string_list(metadata.get("acceptance_criteria"))
    if _is_read_only_audit(metadata):
        if _declares_audit_failed(output):
            return CompletionVerification(
                status="rework_required",
                missing=("read-only audit did not complete",),
                evidence={
                    "output_excerpt": output[:4000],
                    "pr_url": _first_pr_url(output) or external_metadata.get("pr_url"),
                },
            )
        return CompletionVerification(
            status="satisfied",
            evidence={
                "output_excerpt": output[:4000],
                "pr_url": _first_pr_url(output) or external_metadata.get("pr_url"),
                "policy": (
                    "read-only audit nodes may document implementation next steps "
                    "without blocking dependent implementation nodes"
                ),
            },
        )
    text = output.lower()
    unresolved = _extract_unresolved_items(output)
    missing = list(unresolved)

    for criterion in acceptance_criteria:
        if _criterion_is_declared_unfinished(criterion, text):
            missing.append(criterion)

    missing = _dedupe(missing)
    if missing:
        return CompletionVerification(
            status="rework_required",
            missing=tuple(missing),
            evidence={
                "output_excerpt": output[:4000],
                "pr_url": _first_pr_url(output) or external_metadata.get("pr_url"),
                "policy": "reported unfinished work must be fixed on the same DAG node",
            },
        )
    return CompletionVerification(
        status="satisfied",
        evidence={
            "output_excerpt": output[:4000],
            "pr_url": _first_pr_url(output) or external_metadata.get("pr_url"),
        },
    )


def _result_output(metadata: dict[str, object]) -> str:
    for key in ("result_output", "output", "multica_result_output"):
        value = metadata.get(key)
        if isinstance(value, str):
            return value
    result = metadata.get("result")
    if isinstance(result, dict):
        value = result.get("output")
        if isinstance(value, str):
            return value
    return ""


def _is_read_only_audit(metadata: dict[str, object]) -> bool:
    execution_mode = metadata.get("execution_mode")
    node_kind = metadata.get("node_execution_kind")
    node_mode = metadata.get("node_execution_mode")
    tdd_required = metadata.get("tdd_required")
    expected_changes = metadata.get("expected_changes")
    return (
        execution_mode == "planning_only"
        and node_kind == "exploration"
        and node_mode == "planning_only"
        and tdd_required is False
        and isinstance(expected_changes, str)
        and "no production" in expected_changes.lower()
    )


def _declares_audit_failed(output: str) -> bool:
    lowered = output.lower()
    return any(
        phrase in lowered
        for phrase in (
            "audit failed",
            "could not complete the audit",
            "blocked from auditing",
            "unable to inspect",
            "unable to check out",
            "repo checkout failed",
            "repository checkout failed",
        )
    )


def _extract_unresolved_items(output: str) -> list[str]:
    if not output:
        return []
    matches = FOLLOWUP_SECTION_RE.findall(output)
    candidates: list[str] = []
    for section in matches:
        for raw_line in section.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            line = re.sub(r"^[-*]\s*", "", line)
            line = re.sub(r"^\d+[.)]\s*", "", line)
            if _looks_like_unresolved(line):
                candidates.append(line)
    return candidates


def _looks_like_unresolved(line: str) -> bool:
    lowered = line.lower()
    return any(
        phrase in lowered
        for phrase in (
            "add ",
            "create ",
            "include ",
            "update ",
            "team should",
            "if you want",
            "i can ",
            "not included",
            "left ",
            "remaining",
        )
    )


def _criterion_is_declared_unfinished(criterion: str, output: str) -> bool:
    if not output:
        return False
    lowered = criterion.lower()
    has_topic = any(token in output for token in _topic_tokens(lowered))
    has_unfinished_language = any(
        phrase in output
        for phrase in (
            "next steps",
            "follow-up",
            "followup",
            "team should add",
            "i can create",
            "i can author",
            "left migration",
            "not included",
        )
    )
    return has_topic and has_unfinished_language


def _topic_tokens(text: str) -> tuple[str, ...]:
    tokens = []
    if "liquibase" in text or "migration" in text:
        tokens.extend(["liquibase", "migration", "changelog"])
    if "audit" in text:
        tokens.append("audit")
    if "listing" in text or "list" in text or "view" in text:
        tokens.extend(["listing", "view", "list"])
    if "test" in text or "coverage" in text:
        tokens.extend(["test", "coverage"])
    if "frontend" in text or "ui" in text:
        tokens.extend(["frontend", "ui", "webapp"])
    if "mapper" in text or "dto" in text:
        tokens.extend(["mapper", "dto"])
    return tuple(tokens)


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _first_pr_url(output: str) -> str | None:
    match = PR_URL_RE.search(output)
    return match.group(0) if match else None


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = " ".join(value.split())
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result
