from agentic_sdlc_platform.glue.quality_gate import evaluate_completion_quality_gate


def test_quality_gate_is_not_applicable_without_write_or_pr_metadata() -> None:
    result = evaluate_completion_quality_gate(metadata={"execution_mode": "dry_run"})

    assert result.status == "not_applicable"
    assert result.satisfied is True


def test_quality_gate_blocks_write_pr_without_test_evidence() -> None:
    result = evaluate_completion_quality_gate(
        metadata={"execution_mode": "write_pr"},
        expected_pr_reference="dag/dag-1/api",
    )

    assert result.status == "blocked"
    assert result.missing == ("test_evidence",)


def test_quality_gate_requires_contract_tests_for_endpoint_changes() -> None:
    result = evaluate_completion_quality_gate(
        metadata={
            "execution_mode": "write_pr",
            "test_evidence": {
                "unit_tests_passed": True,
                "focused_tests_passed": True,
                "smoke_tests_required": False,
                "contract_tests_required": True,
                "production_files_changed": ["src/agentic_sdlc_platform/api/tasks.py"],
                "test_files_changed": ["tests/test_task_dag_api.py"],
                "pr_body_reference": "dag/dag-1/api",
            },
        },
        expected_pr_reference="dag/dag-1/api",
    )

    assert result.status == "blocked"
    assert result.missing == (
        "contract tests must pass for endpoint/schema/webhook changes",
    )


def test_quality_gate_accepts_complete_test_evidence() -> None:
    result = evaluate_completion_quality_gate(
        metadata={
            "execution_mode": "write_pr",
            "test_evidence": {
                "unit_tests_passed": True,
                "focused_tests_passed": True,
                "smoke_tests_required": False,
                "contract_tests_required": True,
                "contract_tests_passed": True,
                "production_files_changed": ["src/agentic_sdlc_platform/api/tasks.py"],
                "test_files_changed": ["tests/test_task_dag_api.py"],
                "contract_test_files_changed": ["tests/contracts/test_openapi_contract.py"],
                "pr_body_reference": "dag/dag-1/api",
            },
        },
        expected_pr_reference="dag/dag-1/api",
    )

    assert result.status == "satisfied"
    assert result.missing == ()
