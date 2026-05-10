from agentic_sdlc_platform.glue.dag_completion_verifier import verify_node_completion


def test_planning_only_audit_next_steps_do_not_block_completion() -> None:
    result = verify_node_completion(
        node_key="audit_eng_1284_scope",
        node_title="Audit ENG-1284 implementation scope",
        repo="acme-corp/platform-service",
        metadata={
            "execution_mode": "planning_only",
            "node_execution_kind": "exploration",
            "node_execution_mode": "planning_only",
            "tdd_required": False,
            "expected_changes": "No production code changes. Produce notes.",
            "acceptance_criteria": [
                "Audit and document the relevant code paths without production code changes.",
            ],
        },
        external_metadata={
            "result_output": "\n".join(
                [
                    "Summary of what I found.",
                    "Confirmed: the relevant worker uses the existing client adapter.",
                    "Next steps:",
                    "- Add the new capability in the selected module.",
                    "- Add focused tests for the changed behavior.",
                    "Because this run was planning_only, I stopped at audit.",
                ]
            ),
        },
    )

    assert result.status == "satisfied"
    assert result.missing == ()


def test_planning_only_audit_declared_failure_still_blocks_completion() -> None:
    result = verify_node_completion(
        node_key="audit_eng_1284_scope",
        node_title="Audit ENG-1284 implementation scope",
        repo="acme-corp/platform-service",
        metadata={
            "execution_mode": "planning_only",
            "node_execution_kind": "exploration",
            "node_execution_mode": "planning_only",
            "tdd_required": False,
            "expected_changes": "No production code changes. Produce notes.",
            "acceptance_criteria": [
                "Audit and document the relevant code paths without production code changes.",
            ],
        },
        external_metadata={
            "result_output": "Could not complete the audit. Repo checkout failed.",
        },
    )

    assert result.status == "rework_required"
    assert result.missing
