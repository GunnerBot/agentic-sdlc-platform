from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.glue.execution_policy import (
    bounded_graph_context,
    execution_policy_metadata,
    retry_policy_for_mode,
    sanitize_write_metadata,
)


def test_bounded_graph_context_truncates_answer_and_references() -> None:
    context = bounded_graph_context(
        status="available",
        provider="graphify",
        answer="abcdef",
        references=["a.py:1", "b.py:2", "c.py:3"],
        max_chars=3,
        max_references=2,
    )

    assert context == {
        "status": "available",
        "provider": "graphify",
        "answer": "abc\n...[truncated]",
        "answer_chars": 3,
        "original_answer_chars": 6,
        "truncated": True,
        "references": ["a.py:1", "b.py:2"],
        "reference_count": 3,
        "references_truncated": True,
    }


def test_dry_run_retry_policy_and_metadata_remove_write_targets() -> None:
    settings = Settings(
        agent_readonly_max_model_retries=0,
        agent_write_max_model_retries=1,
    )
    metadata = sanitize_write_metadata(
        {
            "execution_mode": "dry_run",
            "expected_branch": "agent/dag/dag-1/node",
            "expected_pr_reference": "dag/dag-1/node",
            "safe": "kept",
        },
        execution_mode="dry_run",
    )

    assert metadata == {"execution_mode": "dry_run", "safe": "kept"}
    assert retry_policy_for_mode(settings, "dry_run") == {
        "mode": "dry_run",
        "max_model_retries": 0,
    }
    assert retry_policy_for_mode(settings, "write_pr") == {
        "mode": "write_pr",
        "max_model_retries": 1,
    }
    assert execution_policy_metadata("dry_run") == {
        "terminal_command_prefix": "rtk",
        "repo_context_policy": "graphstore_first_then_narrow_source_verification",
        "github_write_enabled": False,
    }
