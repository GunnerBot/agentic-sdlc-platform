import re
from dataclasses import dataclass

from agentic_sdlc_platform.ports.issue_tracker import IssueCreateRequest


@dataclass(frozen=True)
class CreateTicketCommand:
    title: str
    repo: str | None = None
    template: str | None = None
    body: str | None = None


def parse_create_ticket(text: str) -> CreateTicketCommand | None:
    match = re.match(r"^/create-ticket\s+(?P<body>.+)$", text.strip(), flags=re.IGNORECASE)
    if not match:
        return None
    raw_body = match.group("body").strip()
    if not raw_body:
        return None

    repo = _extract_token(raw_body, "repo")
    template = _extract_token(raw_body, "type") or _extract_token(raw_body, "template")
    cleaned = _strip_tokens(raw_body)
    title, body = _split_title_and_body(cleaned)
    if not title:
        return None
    return CreateTicketCommand(
        title=title,
        repo=repo,
        template=template,
        body=body,
    )


def build_issue_create_request(
    command: CreateTicketCommand,
    provider: str,
    channel: str,
    sender_id: str,
    message_ts: str | None = None,
    thread_ts: str | None = None,
) -> IssueCreateRequest:
    context_lines = [
        "Created from channel command.",
        f"Provider: {provider}",
        f"Channel: {channel}",
        f"Sender: {sender_id}",
    ]
    if message_ts:
        context_lines.append(f"Message timestamp: {message_ts}")
    if thread_ts:
        context_lines.append(f"Thread timestamp: {thread_ts}")
    if command.repo:
        context_lines.append(f"Repo: {command.repo}")
    if command.template:
        context_lines.append(f"Template: {command.template}")
    if command.body:
        context_lines.extend(["", command.body])

    metadata: dict[str, object] = {
        "provider": provider,
        "channel": channel,
        "sender_id": sender_id,
    }
    if message_ts:
        metadata["message_ts"] = message_ts
    if thread_ts:
        metadata["thread_ts"] = thread_ts
    if command.template:
        metadata["template"] = command.template

    return IssueCreateRequest(
        title=command.title,
        description="\n".join(context_lines),
        repo=command.repo,
        metadata=metadata,
    )


def _extract_token(text: str, name: str) -> str | None:
    match = re.search(rf"\b{name}:(?P<value>[A-Za-z0-9_.\-/]+)\b", text)
    return match.group("value") if match else None


def _strip_tokens(text: str) -> str:
    return re.sub(r"\b(repo|type|template):[A-Za-z0-9_.\-/]+\b", "", text).strip()


def _split_title_and_body(text: str) -> tuple[str, str | None]:
    if "|" not in text:
        return text.strip(), None
    title, body = text.split("|", 1)
    return title.strip(), body.strip() or None
