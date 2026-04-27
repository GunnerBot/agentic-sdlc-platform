import json

import httpx

from agentic_sdlc_platform.adapters.telegram import TelegramClient
from agentic_sdlc_platform.core.config import Settings


async def test_telegram_client_sends_message() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            status_code=200,
            json={"ok": True, "result": {"message_id": 42}},
        )

    message_id = await TelegramClient(
        Settings(
            telegram_bot_token="bot-token",
            telegram_api_base_url="https://telegram.local",
        ),
        transport=httpx.MockTransport(handler),
    ).send_message(chat_id="-1001234567890", text="Agent found the answer.")

    assert captured_request is not None
    assert str(captured_request.url) == "https://telegram.local/botbot-token/sendMessage"
    assert json.loads(captured_request.content) == {
        "chat_id": "-1001234567890",
        "text": "Agent found the answer.",
    }
    assert message_id == 42
