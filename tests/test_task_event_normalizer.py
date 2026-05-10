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
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "url": "https://linear.app/acme/issue/ENG-1284",
                "description": "Create the agentic SDLC bridge.",
                "labels": {
                    "nodes": [
                        {"name": "agent"},
                        {"name": "repo:erp-service"},
                        {"name": "type:feature"},
                    ]
                },
            },
        },
    )

    assert task_event is not None
    assert task_event.source == "linear"
    assert task_event.external_id == "ENG-1284"
    assert task_event.title == "Build webhook bridge"
    assert task_event.repo == "erp-service"
    assert task_event.dag_template == "feature"
    assert task_event.url == "https://linear.app/acme/issue/ENG-1284"
    assert task_event.execution_mode == "dry_run"


def test_linear_execution_mode_labels_are_not_external_write_controls() -> None:
    task_event = TaskEventNormalizer().normalize(
        source="linear",
        event_type="Issue",
        payload={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "labels": {"nodes": [{"name": "mode:write_pr"}]},
            },
        },
    )

    assert task_event is not None
    assert task_event.execution_mode == "dry_run"


def test_normalizes_linear_execution_mode_from_configured_default() -> None:
    task_event = TaskEventNormalizer(default_execution_mode="planning_only").normalize(
        source="linear",
        event_type="Issue",
        payload={
            "type": "Issue",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "labels": {"nodes": []},
            },
        },
    )

    assert task_event is not None
    assert task_event.execution_mode == "planning_only"


def test_linear_assignment_filter_requires_configured_agent_assignee() -> None:
    normalizer = TaskEventNormalizer(linear_agent_user_id="agent-user-1")

    unmatched = normalizer.normalize(
        source="linear",
        event_type="Issue",
        payload={
            "type": "Issue",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "someone-else"},
            },
        },
    )
    matched = normalizer.normalize(
        source="linear",
        event_type="Issue",
        payload={
            "type": "Issue",
            "data": {
                "id": "issue-id-1",
                "identifier": "ENG-1284",
                "title": "Build webhook bridge",
                "assignee": {"id": "agent-user-1"},
            },
        },
    )

    assert unmatched is None
    assert matched is not None
    assert matched.issue_id == "issue-id-1"


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


def test_normalizes_github_pull_request_update_from_branch_ticket_key() -> None:
    task_update = TaskEventNormalizer().normalize_update(
        source="github",
        event_type="pull_request",
        payload={
            "action": "opened",
            "pull_request": {
                "number": 17,
                "title": "ENG-1284 Build webhook bridge",
                "html_url": "https://github.com/GunnerBot/agentic-sdlc-platform/pull/17",
                "head": {"ref": "agent/ENG-1284-build-webhook-bridge"},
                "body": "Implements ENG-1284.",
                "merged": False,
            },
            "repository": {"full_name": "GunnerBot/agentic-sdlc-platform"},
        },
    )

    assert task_update is not None
    assert task_update.source == "github"
    assert task_update.external_id == "ENG-1284"
    assert task_update.status == "pr_open"
    assert task_update.repo == "GunnerBot/agentic-sdlc-platform"
    assert task_update.metadata == {
        "head_branch": "agent/ENG-1284-build-webhook-bridge",
        "pull_request": 17,
        "url": "https://github.com/GunnerBot/agentic-sdlc-platform/pull/17",
    }


def test_normalizes_github_pull_request_update_from_dag_node_reference() -> None:
    dag_id = "11111111-2222-3333-4444-555555555555"

    task_update = TaskEventNormalizer().normalize_update(
        source="github",
        event_type="pull_request",
        payload={
            "action": "opened",
            "pull_request": {
                "number": 17,
                "title": "Add implementation node",
                "head": {"ref": f"agent/dag/{dag_id}/implement"},
                "body": f"Node: dag:{dag_id}:implement",
                "merged": False,
            },
            "repository": {"full_name": "GunnerBot/agentic-sdlc-platform"},
        },
    )

    assert task_update is not None
    assert task_update.external_id == f"{dag_id}:implement"
    assert task_update.status == "pr_open"
    assert task_update.dag_id == dag_id
    assert task_update.dag_node_key == "implement"


def test_normalizes_github_pull_request_merged_status() -> None:
    task_update = TaskEventNormalizer().normalize_update(
        source="github",
        event_type="pull_request",
        payload={
            "action": "closed",
            "pull_request": {
                "number": 17,
                "title": "Build webhook bridge",
                "head": {"ref": "agent/ENG-1284-build-webhook-bridge"},
                "merged": True,
            },
            "repository": {"full_name": "GunnerBot/agentic-sdlc-platform"},
        },
    )

    assert task_update is not None
    assert task_update.status == "merged"
