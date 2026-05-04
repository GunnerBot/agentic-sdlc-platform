from agentic_sdlc_platform.glue.adversarial_review import (
    adversarial_review_approved,
    normalize_adversarial_review,
)


def test_normalize_adversarial_review_blocks_on_revise_verdict() -> None:
    review = normalize_adversarial_review(
        {
            "phase": "implementation",
            "turn": 2,
            "reviewer": "adversarial-supervisor",
            "review": {
                "verdict": "revise",
                "score": {"overall": 61},
                "summary": "Tests are missing.",
                "issues": [
                    {
                        "id": "missing-tests",
                        "blocking": True,
                        "description": "No focused tests were added.",
                    }
                ],
            },
        },
        required=True,
    )

    assert review["required"] is True
    assert review["status"] == "revise"
    assert review["approved"] is False
    assert review["score"] == 61.0
    assert review["blocking_issue_count"] == 1
    assert review["blocking_issues"] == [
        {
            "id": "missing-tests",
            "description": "No focused tests were added.",
        }
    ]


def test_normalize_adversarial_review_accepts_partially_approved_without_blockers() -> None:
    review = normalize_adversarial_review(
        {
            "phase": "verification",
            "review": {
                "verdict": "partially-approved",
                "score": {"overall": 83.5},
                "issues": [{"id": "nit", "blocking": False}],
            },
        },
        required=True,
    )

    assert review["status"] == "partially_approved"
    assert review["approved"] is True
    assert review["blocking_issue_count"] == 0


def test_adversarial_review_approval_never_ignores_blocking_issues() -> None:
    assert (
        adversarial_review_approved(
            {
                "adversarial_review": {
                    "approved": True,
                    "status": "approved",
                    "blocking_issue_count": 1,
                }
            }
        )
        is False
    )
