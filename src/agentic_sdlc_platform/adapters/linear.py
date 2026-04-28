import httpx

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.issue_tracker import (
    IssueAttachment,
    IssueComment,
    IssueContext,
    IssueCreateRequest,
    IssueCreateResponse,
    IssueTrackerError,
    IssueTrackerReply,
    IssueTrackerUpdate,
)


class LinearIssueAdapter:
    provider = "linear"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def create_issue(self, request: IssueCreateRequest) -> IssueCreateResponse:
        self._ensure_configured()
        team_id = request.team_id or self._settings.linear_team_id
        if not team_id:
            raise IssueTrackerError("linear team ID is not configured")

        payload = {
            "query": """
mutation AgentIssueCreate($teamId: String!, $title: String!, $description: String!) {
  issueCreate(input: {teamId: $teamId, title: $title, description: $description}) {
    success
    issue {
      id
      identifier
      url
    }
  }
}
""",
            "variables": {
                "teamId": team_id,
                "title": request.title,
                "description": request.description,
            },
        }
        try:
            response_payload = await self._post_graphql(payload)
        except httpx.HTTPError as exc:
            raise IssueTrackerError("linear create_issue failed") from exc

        issue = (
            response_payload.get("data", {})
            .get("issueCreate", {})
            .get("issue", {})
        )
        issue_id = issue.get("id")
        external_id = issue.get("identifier")
        url = issue.get("url")
        if not isinstance(issue_id, str) or not isinstance(external_id, str):
            raise IssueTrackerError("linear create_issue returned invalid response")
        return IssueCreateResponse(
            issue_id=issue_id,
            external_id=external_id,
            url=url if isinstance(url, str) else None,
        )

    async def mark_task_queued(self, update: IssueTrackerUpdate) -> None:
        self._ensure_configured()

        body = (
            f"Agent task queued for {update.external_id}. "
            f"Internal task: {update.internal_task_id}."
        )
        if update.orchestrator_task_id:
            body += f" Multica task: {update.orchestrator_task_id}."

        await self._create_comment(issue_id=update.issue_id, body=body)

    async def reply(self, reply: IssueTrackerReply) -> None:
        await self._create_comment(issue_id=reply.issue_id, body=reply.body)

    async def get_issue_context(self, issue_id: str) -> IssueContext:
        self._ensure_configured()
        payload = {
            "query": """
query AgentIssueContext($issueId: String!) {
  issue(id: $issueId) {
    id
    identifier
    title
    description
    url
    attachments(first: 50) {
      nodes {
        id
        title
        subtitle
        url
        metadata
      }
    }
    comments(first: 50) {
      nodes {
        id
        body
        user {
          id
          name
        }
      }
    }
  }
}
""",
            "variables": {"issueId": issue_id},
        }
        try:
            response_payload = await self._post_graphql(payload)
        except httpx.HTTPError as exc:
            raise IssueTrackerError("linear get_issue_context failed") from exc

        issue = response_payload.get("data", {}).get("issue", {})
        if not isinstance(issue, dict):
            raise IssueTrackerError("linear get_issue_context returned invalid response")
        hydrated_issue_id = _str_value(issue.get("id")) or issue_id
        return IssueContext(
            issue_id=hydrated_issue_id,
            identifier=_str_value(issue.get("identifier")),
            title=_str_value(issue.get("title")),
            description=_str_value(issue.get("description")),
            url=_str_value(issue.get("url")),
            attachments=_parse_attachments(issue.get("attachments")),
            comments=_parse_comments(issue.get("comments")),
        )

    async def _create_comment(self, issue_id: str, body: str) -> None:
        payload = {
            "query": """
mutation AgentTaskQueued($issueId: String!, $body: String!) {
  commentCreate(input: {issueId: $issueId, body: $body}) {
    success
  }
}
""",
            "variables": {
                "issueId": issue_id,
                "body": body,
            },
        }
        try:
            await self._post_graphql(payload)
        except httpx.HTTPError as exc:
            raise IssueTrackerError("linear mark_task_queued failed") from exc

    def _ensure_configured(self) -> None:
        if not self._settings.linear_http_enabled:
            raise IssueTrackerError("linear HTTP is disabled")
        if not self._settings.linear_base_url:
            raise IssueTrackerError("linear base URL is not configured")
        if not self._settings.linear_api_key:
            raise IssueTrackerError("linear API key is not configured")

    async def _post_graphql(self, payload: dict[str, object]) -> dict[str, object]:
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
                response_payload = response.json()
        except httpx.HTTPError:
            raise
        if not isinstance(response_payload, dict):
            raise IssueTrackerError("linear returned invalid response")
        return response_payload


def _parse_attachments(value: object) -> list[IssueAttachment]:
    attachments = []
    for node in _nodes(value):
        metadata = node.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        content_type = (
            _str_value(node.get("contentType"))
            or _str_value(node.get("mimeType"))
            or _str_value(metadata.get("contentType"))
            or _str_value(metadata.get("mimeType"))
        )
        attachments.append(
            IssueAttachment(
                id=_str_value(node.get("id")),
                title=_str_value(node.get("title")),
                url=_str_value(node.get("url")),
                content_type=content_type,
                content=_str_value(
                    node.get("content")
                    or node.get("body")
                    or node.get("text")
                    or node.get("description")
                ),
                metadata=metadata,
            )
        )
    return attachments


def _parse_comments(value: object) -> list[IssueComment]:
    comments = []
    for node in _nodes(value):
        user = node.get("user")
        user = user if isinstance(user, dict) else {}
        comments.append(
            IssueComment(
                id=_str_value(node.get("id")),
                body=_str_value(node.get("body")),
                actor=_str_value(user.get("id")) or _str_value(user.get("name")),
            )
        )
    return comments


def _nodes(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        nodes = value.get("nodes")
        if isinstance(nodes, list):
            return [node for node in nodes if isinstance(node, dict)]
    if isinstance(value, list):
        return [node for node in value if isinstance(node, dict)]
    return []


def _str_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
