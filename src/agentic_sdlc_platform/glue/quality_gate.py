from __future__ import annotations

from dataclasses import dataclass, field

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
    execution_mode = _str(merged.get("execution_mode"))
    return bool(
        execution_mode == "write_pr"
        or merged.get("expected_pr_reference")
        or merged.get("expected_branch")
        or merged.get("pr_url")
        or merged.get("pr_number")
        or merged.get("pull_request")
        or _dict(merged.get("pr_plan"))
    )


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

    evidence = _test_evidence(merged)
    missing: list[str] = []
    if not evidence:
        return QualityGateResult(
            status="blocked",
            missing=("test_evidence",),
            evidence={},
        )

    for key, label in (
        ("unit_tests_passed", "unit tests must pass"),
        ("focused_tests_passed", "focused tests must pass"),
    ):
        if evidence.get(key) is not True:
            missing.append(label)

    smoke_required = evidence.get("smoke_tests_required", True)
    if smoke_required is not False and evidence.get("smoke_tests_passed") is not True:
        missing.append("configured smoke tests must pass")

    contract_required = (
        evidence.get("contract_tests_required") is True
        or bool(_string_list(evidence.get("endpoint_files_changed")))
        or bool(_string_list(evidence.get("api_contract_files_changed")))
    )
    if contract_required and evidence.get("contract_tests_passed") is not True:
        missing.append("contract tests must pass for endpoint/schema/webhook changes")

    production_files = _string_list(evidence.get("production_files_changed"))
    test_files = _string_list(evidence.get("test_files_changed"))
    if production_files and not test_files:
        missing.append("changed production files must have relevant tests in the same PR")

    if expected_pr_reference and not _pr_body_has_reference(
        evidence=evidence,
        external_metadata=external_metadata,
        expected_pr_reference=expected_pr_reference,
    ):
        missing.append(f"PR body must include {expected_pr_reference}")

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


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


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
