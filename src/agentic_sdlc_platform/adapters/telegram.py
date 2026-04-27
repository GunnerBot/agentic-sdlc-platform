import httpx

from agentic_sdlc_platform.core.config import Settings


class TelegramClientError(RuntimeError):
    pass


class TelegramClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def send_message(self, chat_id: str, text: str) -> int | None:
        if not self._settings.telegram_bot_token:
            return None

        try:
            async with httpx.AsyncClient(
                base_url=self._settings.telegram_api_base_url,
                timeout=self._settings.telegram_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    f"/bot{self._settings.telegram_bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise TelegramClientError("telegram sendMessage failed") from exc

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise TelegramClientError("telegram sendMessage returned invalid response")
        result = payload.get("result")
        if not isinstance(result, dict):
            return None
        message_id = result.get("message_id")
        return message_id if isinstance(message_id, int) else None
