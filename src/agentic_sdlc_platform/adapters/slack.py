import re

import httpx

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.glue.ticket_command import TicketThreadContext


class SlackClientError(RuntimeError):
    pass


class SlackClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def fetch_thread_context(
        self,
        channel: str,
        thread_ts: str,
    ) -> TicketThreadContext | None:
        if not self._settings.slack_bot_token:
            return None

        try:
            async with httpx.AsyncClient(
                base_url=self._settings.slack_api_base_url,
                timeout=self._settings.slack_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    "/conversations.replies",
                    params={"channel": channel, "ts": thread_ts},
                    headers={"Authorization": f"Bearer {self._settings.slack_bot_token}"},
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise SlackClientError("slack conversations.replies failed") from exc

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise SlackClientError("slack conversations.replies returned invalid response")
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return None

        lines = []
        first_text = None
        for message in messages:
            if not isinstance(message, dict):
                continue
            text = _clean_text(message.get("text"))
            if not text:
                continue
            if first_text is None:
                first_text = text
            actor = message.get("user") or message.get("bot_id") or "unknown"
            lines.append(f"{actor}: {text}")
        if not lines or not first_text:
            return None

        return TicketThreadContext(
            title=_title_from_text(first_text),
            transcript="\n".join(lines),
            message_count=len(lines),
        )


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"<@[A-Z0-9]+>\s*", "", value)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _title_from_text(text: str) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= 80:
        return cleaned
    return cleaned[:77].rstrip() + "..."
