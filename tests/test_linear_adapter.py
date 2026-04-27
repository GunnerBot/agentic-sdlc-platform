import json

import httpx
import pytest

from agentic_sdlc_platform.adapters.linear import LinearIssueAdapter
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.issue_tracker import IssueTrackerError, IssueTrackerUpdate


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
