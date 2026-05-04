from __future__ import annotations

from agentic_sdlc_platform.core.config import Settings

READ_ONLY_QUESTION = "read_only_question"
PLANNING_ONLY = "planning_only"
DRY_RUN = "dry_run"
WRITE_PR = "write_pr"
EXECUTION_MODES = {READ_ONLY_QUESTION, PLANNING_ONLY, DRY_RUN, WRITE_PR}
WRITE_FIELDS = {
    "expected_branch",
    "expected_pr_reference",
    "expected_pr_body_marker",
}


def code_generation_policy() -> dict[str, object]:
    return {
        "branching_model": "trunk_based_development",
        "base_branch": "trunk_or_default_branch",
        "pr_size": "small_ordered_prs",
        "common_code_change_policy": (
            "Changes to shared/common/existing behavior must be guarded by a "
            "feature flag or equivalent compatibility gate unless the task "
            "explicitly states that a breaking global change is intended."
        ),
        "feature_flag_required_for_common_code": True,
        "tests_policy": "implementation_and_relevant_tests_same_pr",
        "test_first_required": True,
        "test_first_policy": (
            "For write-capable DAG nodes, create or update relevant tests before "
            "production code edits, then keep tests and implementation in the same PR."
        ),
        "changed_file_test_gate": True,
        "contract_tests_required_for_api_changes": True,
        "open_pr_allowed_only_after_tests_passing": True,
        "completion_gate": (
            "Do not mark a node completed or fixed until unit, focused, contract-when-relevant, "
            "and configured smoke checks pass, and test evidence is persisted."
        ),
        "merge_order_policy": (
            "merge PRs in DAG dependency order; do not merge a dependent PR first"
        ),
    }


def normalize_execution_mode(value: object, *, default: str = DRY_RUN) -> str:
    if not isinstance(value, str):
        return default
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "readonly": READ_ONLY_QUESTION,
        "read_only": READ_ONLY_QUESTION,
        "question": READ_ONLY_QUESTION,
        "plan": PLANNING_ONLY,
        "planning": PLANNING_ONLY,
        "dryrun": DRY_RUN,
        "dry": DRY_RUN,
        "write": WRITE_PR,
        "pr": WRITE_PR,
        "open_pr": WRITE_PR,
        "create_pr": WRITE_PR,
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in EXECUTION_MODES else default


def github_write_enabled(execution_mode: str) -> bool:
    return normalize_execution_mode(execution_mode) == WRITE_PR


def execution_policy_metadata(execution_mode: str) -> dict[str, object]:
    return {
        "terminal_command_prefix": "rtk",
        "repo_context_policy": "graphstore_first_then_narrow_source_verification",
        "github_write_enabled": github_write_enabled(execution_mode),
    }


def retry_policy_for_mode(settings: Settings, execution_mode: str) -> dict[str, object]:
    mode = normalize_execution_mode(
        execution_mode,
        default=settings.agent_default_execution_mode,
    )
    return {
        "mode": mode,
        "max_model_retries": (
            settings.agent_write_max_model_retries
            if github_write_enabled(mode)
            else settings.agent_readonly_max_model_retries
        ),
    }


def sanitize_write_metadata(
    metadata: dict[str, object],
    *,
    execution_mode: str,
) -> dict[str, object]:
    if github_write_enabled(execution_mode):
        return dict(metadata)
    return {key: value for key, value in metadata.items() if key not in WRITE_FIELDS}


def bounded_graph_context(
    *,
    status: str,
    provider: str | None = None,
    answer: str | None = None,
    references: list[object] | None = None,
    reason: str | None = None,
    max_chars: int = 4000,
    max_references: int = 10,
) -> dict[str, object]:
    context: dict[str, object] = {"status": status}
    if provider:
        context["provider"] = provider
    if reason:
        context["reason"] = reason
    if answer is not None:
        original_chars = len(answer)
        safe_limit = max(0, max_chars)
        context["answer"] = (
            answer
            if original_chars <= safe_limit
            else f"{answer[:safe_limit]}\n...[truncated]"
        )
        context["answer_chars"] = min(original_chars, safe_limit)
        context["original_answer_chars"] = original_chars
        context["truncated"] = original_chars > safe_limit
    if references is not None:
        safe_ref_limit = max(0, max_references)
        context["references"] = references[:safe_ref_limit]
        context["reference_count"] = len(references)
        context["references_truncated"] = len(references) > safe_ref_limit
    return context
