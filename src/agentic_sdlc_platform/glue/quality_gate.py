from __future__ import annotations

from dataclasses import dataclass, field

from agentic_sdlc_platform.glue.adversarial_review import (
    adversarial_review_approved,
    adversarial_review_required,
)

TEST_EVIDENCE_KEYS = ("test_evidence", "quality_evidence")


@dataclass(frozen=True)
class QualityGateResult:
    status: str
    missing: tuple[str, ...] = field(default_factory=tuple)
    evidence: dict[str, object] = field(default_factory=dict)

    @property
    def satisfied(self) -> bool:
        return self.status in {"satisfied", "not_applicable"}


def quality_gate_applies(
    *,
    metadata: dict[str, object],
    external_metadata: dict[str, object] | None = None,
) -> bool:
    external_metadata = external_metadata or {}
    merged = {**metadata, **external_metadata}
    return _write_quality_applies(merged) or adversarial_review_required(merged)


def evaluate_completion_quality_gate(
    *,
    metadata: dict[str, object],
    external_metadata: dict[str, object] | None = None,
    expected_pr_reference: str | None = None,
) -> QualityGateResult:
    external_metadata = external_metadata or {}
    merged = {**metadata, **external_metadata}
    if not quality_gate_applies(metadata=metadata, external_metadata=external_metadata):
        return QualityGateResult(status="not_applicable")

    write_quality_applies = _write_quality_applies(merged)
    evidence = _test_evidence(merged) if write_quality_applies else {}
    missing: list[str] = []
    if write_quality_applies:
        expected_branch = _str(merged.get("expected_branch"))
        expected_pr_reference = expected_pr_reference or _str(
            merged.get("expected_pr_reference")
        )
        if not _pr_url_present(evidence=evidence, external_metadata=external_metadata):
            missing.append("GitHub PR URL is required for approved write execution")
        if expected_branch and not _branch_matches(
            expected_branch=expected_branch,
            evidence=evidence,
            external_metadata=external_metadata,
        ):
            missing.append(f"PR branch must be {expected_branch}")
        if not evidence:
            missing.append("test_evidence")
        else:
            for key, label in (
                (
                    "failing_tests_observed",
                    "failing test evidence from the test-first red step is required",
                ),
                ("unit_tests_passed", "unit tests must pass"),
                ("focused_tests_passed", "focused tests must pass"),
            ):
                if key == "failing_tests_observed":
                    if not _failing_test_evidence_present(evidence):
                        missing.append(label)
                    continue
                if evidence.get(key) is not True:
                    missing.append(label)

            smoke_required = evidence.get("smoke_tests_required", True)
            if (
                smoke_required is not False
                and evidence.get("smoke_tests_passed") is not True
            ):
                missing.append("configured smoke tests must pass")

            contract_required = (
                evidence.get("contract_tests_required") is True
                or bool(_string_list(evidence.get("endpoint_files_changed")))
                or bool(_string_list(evidence.get("api_contract_files_changed")))
            )
            if contract_required and evidence.get("contract_tests_passed") is not True:
                missing.append(
                    "contract tests must pass for endpoint/schema/webhook changes"
                )

            production_files = _string_list(evidence.get("production_files_changed"))
            test_files = _string_list(evidence.get("test_files_changed"))
            if not production_files:
                missing.append("changed production files must be reported")
            if not test_files:
                missing.append("relevant test files must be reported")
            if production_files and not test_files:
                missing.append(
                    "changed production files must have relevant tests in the same PR"
                )

            if expected_pr_reference and not _pr_body_has_reference(
                evidence=evidence,
                external_metadata=external_metadata,
                expected_pr_reference=expected_pr_reference,
            ):
                missing.append(f"PR body must include {expected_pr_reference}")

    if adversarial_review_required(merged) and not adversarial_review_approved(merged):
        missing.append("adversarial review must approve this DAG node")

    return QualityGateResult(
        status="satisfied" if not missing else "blocked",
        missing=tuple(_dedupe(missing)),
        evidence=evidence,
    )


def quality_gate_metadata(result: QualityGateResult) -> dict[str, object]:
    return {
        "status": result.status,
        "missing": list(result.missing),
        "evidence": result.evidence,
    }


def _write_quality_applies(merged: dict[str, object]) -> bool:
    execution_mode = _str(merged.get("execution_mode"))
    return bool(
        execution_mode == "write_pr"
        or merged.get("expected_pr_reference")
        or merged.get("expected_branch")
        or merged.get("pr_url")
        or merged.get("pr_number")
        or merged.get("pull_request")
    )


def _test_evidence(metadata: dict[str, object]) -> dict[str, object]:
    for key in TEST_EVIDENCE_KEYS:
        value = _dict(metadata.get(key))
        if value:
            return value
    completion = _dict(metadata.get("completion_verification"))
    for key in TEST_EVIDENCE_KEYS:
        value = _dict(completion.get(key))
        if value:
            return value
    return {}


def _pr_body_has_reference(
    *,
    evidence: dict[str, object],
    external_metadata: dict[str, object],
    expected_pr_reference: str,
) -> bool:
    if evidence.get("pr_body_has_dag_reference") is True:
        return True
    if external_metadata.get("pr_body_has_dag_reference") is True:
        return True
    return (
        evidence.get("pr_body_reference") == expected_pr_reference
        or external_metadata.get("pr_body_reference") == expected_pr_reference
    )


def _pr_url_present(
    *,
    evidence: dict[str, object],
    external_metadata: dict[str, object],
) -> bool:
    for value in (
        evidence.get("pr_url"),
        evidence.get("pull_request_url"),
        evidence.get("pull_request"),
        external_metadata.get("pr_url"),
        external_metadata.get("pull_request_url"),
        external_metadata.get("url"),
        external_metadata.get("pull_request"),
    ):
        if _looks_like_pr_url(_str(value)):
            return True
    return False


def _looks_like_pr_url(value: str | None) -> bool:
    if not value:
        return False
    return "/pull/" in value and value.startswith(("https://", "http://"))


def _branch_matches(
    *,
    expected_branch: str,
    evidence: dict[str, object],
    external_metadata: dict[str, object],
) -> bool:
    for key in ("branch", "branch_name", "head_branch", "pr_branch"):
        if evidence.get(key) == expected_branch or external_metadata.get(key) == expected_branch:
            return True
    return False


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _failing_test_evidence_present(evidence: dict[str, object]) -> bool:
    if (
        evidence.get("failing_tests_observed") is True
        or evidence.get("red_tests_observed") is True
        or evidence.get("test_first_failure_observed") is True
    ):
        return True
    return bool(
        _str(evidence.get("failing_test_command"))
        and _str(evidence.get("failing_test_output"))
    )


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
