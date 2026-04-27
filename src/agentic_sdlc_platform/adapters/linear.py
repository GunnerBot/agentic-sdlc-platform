import httpx

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.issue_tracker import IssueTrackerError, IssueTrackerUpdate


class LinearIssueAdapter:
    provider = "linear"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def mark_task_queued(self, update: IssueTrackerUpdate) -> None:
        if not self._settings.linear_http_enabled:
            raise IssueTrackerError("linear HTTP is disabled")
        if not self._settings.linear_base_url:
            raise IssueTrackerError("linear base URL is not configured")
        if not self._settings.linear_api_key:
            raise IssueTrackerError("linear API key is not configured")

        body = (
            f"Agent task queued for {update.external_id}. "
            f"Internal task: {update.internal_task_id}."
        )
        if update.orchestrator_task_id:
            body += f" Multica task: {update.orchestrator_task_id}."

        payload = {
            "query": """
mutation AgentTaskQueued($issueId: String!, $body: String!) {
  commentCreate(input: {issueId: $issueId, body: $body}) {
    success
  }
}
""",
            "variables": {
                "issueId": update.issue_id,
                "body": body,
            },
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.linear_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self._settings.linear_base_url,
                    json=payload,
                    headers={"Authorization": self._settings.linear_api_key},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise IssueTrackerError("linear mark_task_queued failed") from exc
