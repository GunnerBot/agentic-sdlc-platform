import json

import httpx
import pytest

from agentic_sdlc_platform.adapters.linear import LinearIssueAdapter
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.issue_tracker import (
    IssueCreateRequest,
    IssueTrackerError,
    IssueTrackerReply,
    IssueTrackerUpdate,
)


async def test_linear_adapter_blocks_when_http_disabled() -> None:
    adapter = LinearIssueAdapter(Settings(linear_http_enabled=False))

    with pytest.raises(IssueTrackerError, match="linear HTTP is disabled"):
        await adapter.mark_task_queued(
            IssueTrackerUpdate(
                issue_id="issue-id-1",
                external_id="OS-1284",
                internal_task_id="task-1",
                orchestrator_task_id="multica-task-1",
            )
        )


async def test_linear_adapter_posts_agent_queued_comment() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            status_code=200,
            json={"data": {"commentCreate": {"success": True}}},
        )

    adapter = LinearIssueAdapter(
        Settings(
            linear_http_enabled=True,
            linear_base_url="https://linear.local/graphql",
            linear_api_key="test-key",
        ),
        transport=httpx.MockTransport(handler),
    )

    await adapter.mark_task_queued(
        IssueTrackerUpdate(
            issue_id="issue-id-1",
            external_id="OS-1284",
            internal_task_id="task-1",
            orchestrator_task_id="multica-task-1",
        )
    )

    assert captured_request is not None
    assert str(captured_request.url) == "https://linear.local/graphql"
    assert captured_request.headers["authorization"] == "test-key"
    payload = json.loads(captured_request.content)
    assert payload["variables"] == {
        "issueId": "issue-id-1",
        "body": (
            "Agent task queued for OS-1284. "
            "Internal task: task-1. Multica task: multica-task-1."
        ),
    }


async def test_linear_adapter_posts_agent_reply_comment() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            status_code=200,
            json={"data": {"commentCreate": {"success": True}}},
        )

    adapter = LinearIssueAdapter(
        Settings(
            linear_http_enabled=True,
            linear_base_url="https://linear.local/graphql",
            linear_api_key="test-key",
        ),
        transport=httpx.MockTransport(handler),
    )

    await adapter.reply(
        IssueTrackerReply(
            issue_id="issue-id-1",
            body="I will check inventory allocation first.",
        )
    )

    assert captured_request is not None
    payload = json.loads(captured_request.content)
    assert payload["variables"] == {
        "issueId": "issue-id-1",
        "body": "I will check inventory allocation first.",
    }


async def test_linear_adapter_creates_issue() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            status_code=200,
            json={
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {
                            "id": "issue-id-1",
                            "identifier": "OS-1284",
                            "url": "https://linear.app/keychain/issue/OS-1284",
                        },
                    }
                }
            },
        )

    adapter = LinearIssueAdapter(
        Settings(
            linear_http_enabled=True,
            linear_base_url="https://linear.local/graphql",
            linear_api_key="test-key",
            linear_team_id="team-id-1",
        ),
        transport=httpx.MockTransport(handler),
    )

    response = await adapter.create_issue(
        IssueCreateRequest(
            title="Add FEFO allocation support",
            description="Created from Slack.",
        )
    )

    assert response.issue_id == "issue-id-1"
    assert response.external_id == "OS-1284"
    assert response.url == "https://linear.app/keychain/issue/OS-1284"
    assert captured_request is not None
    payload = json.loads(captured_request.content)
    assert payload["variables"] == {
        "teamId": "team-id-1",
        "title": "Add FEFO allocation support",
        "description": "Created from Slack.",
    }


async def test_linear_adapter_fetches_issue_context() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            status_code=200,
            json={
                "data": {
                    "issue": {
                        "id": "issue-id-1",
                        "identifier": "OS-3001",
                        "title": "Support dynamic form titles",
                        "description": "## Repositories\n- webapp-monorepo",
                        "url": "https://linear.app/keychain/issue/OS-3001",
                        "attachments": {
                            "nodes": [
                                {
                                    "id": "attachment-1",
                                    "title": "form-title.png",
                                    "url": "https://linear.local/form-title.png",
                                    "metadata": {"contentType": "image/png"},
                                }
                            ]
                        },
                        "comments": {
                            "nodes": [
                                {
                                    "id": "comment-1",
                                    "body": "Use the latest Figma frame.",
                                    "user": {"id": "user-1", "name": "A. User"},
                                }
                            ]
                        },
                    }
                }
            },
        )

    adapter = LinearIssueAdapter(
        Settings(
            linear_http_enabled=True,
            linear_base_url="https://linear.local/graphql",
            linear_api_key="test-key",
        ),
        transport=httpx.MockTransport(handler),
    )

    context = await adapter.get_issue_context("issue-id-1")

    assert captured_request is not None
    payload = json.loads(captured_request.content)
    assert payload["variables"] == {"issueId": "issue-id-1"}
    assert context.issue_id == "issue-id-1"
    assert context.identifier == "OS-3001"
    assert context.description == "## Repositories\n- webapp-monorepo"
    assert context.attachments is not None
    assert context.attachments[0].title == "form-title.png"
    assert context.attachments[0].content_type == "image/png"
    assert context.comments is not None
    assert context.comments[0].actor == "user-1"


async def test_linear_adapter_raises_structured_error_for_failure() -> None:
    adapter = LinearIssueAdapter(
        Settings(
            linear_http_enabled=True,
            linear_base_url="https://linear.local/graphql",
            linear_api_key="test-key",
        ),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status_code=500, json={"error": "boom"})
        ),
    )

    with pytest.raises(IssueTrackerError, match="linear mark_task_queued failed"):
        await adapter.mark_task_queued(
            IssueTrackerUpdate(
                issue_id="issue-id-1",
                external_id="OS-1284",
                internal_task_id="task-1",
                orchestrator_task_id=None,
            )
        )
