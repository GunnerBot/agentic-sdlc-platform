from agentic_sdlc_platform.glue.task_event_normalizer import TaskEventNormalizer


def test_normalizes_linear_issue_payload_to_task_event() -> None:
    task_event = TaskEventNormalizer().normalize(
        source="linear",
        event_type="Issue",
        payload={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "OS-1284",
                "title": "Build webhook bridge",
                "url": "https://linear.app/keychain/issue/OS-1284",
                "description": "Create the agentic SDLC bridge.",
                "labels": {
                    "nodes": [
                        {"name": "agent"},
                        {"name": "repo:keychain-os-erp"},
                    ]
                },
            },
        },
    )

    assert task_event is not None
    assert task_event.source == "linear"
    assert task_event.external_id == "OS-1284"
    assert task_event.title == "Build webhook bridge"
    assert task_event.repo == "keychain-os-erp"
    assert task_event.url == "https://linear.app/keychain/issue/OS-1284"


def test_normalizes_github_agent_labeled_issue_to_task_event() -> None:
    task_event = TaskEventNormalizer().normalize(
        source="github",
        event_type="issues",
        payload={
            "action": "labeled",
            "issue": {
                "number": 42,
                "title": "Add channel router",
                "html_url": "https://github.com/GunnerBot/agentic-sdlc-platform/issues/42",
                "body": "Route Slack Q&A to Hermes.",
                "labels": [{"name": "agent"}, {"name": "repo:agentic-sdlc-platform"}],
            },
            "repository": {"full_name": "GunnerBot/agentic-sdlc-platform"},
        },
    )

    assert task_event is not None
    assert task_event.source == "github"
    assert task_event.external_id == "GunnerBot/agentic-sdlc-platform#42"
    assert task_event.title == "Add channel router"
    assert task_event.repo == "GunnerBot/agentic-sdlc-platform"
    assert task_event.url == "https://github.com/GunnerBot/agentic-sdlc-platform/issues/42"


def test_ignores_github_issue_without_agent_label() -> None:
    task_event = TaskEventNormalizer().normalize(
        source="github",
        event_type="issues",
        payload={
            "action": "opened",
            "issue": {
                "number": 42,
                "title": "Untriaged issue",
                "labels": [{"name": "bug"}],
            },
            "repository": {"full_name": "GunnerBot/agentic-sdlc-platform"},
        },
    )

    assert task_event is None
