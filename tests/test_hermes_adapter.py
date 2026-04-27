import json

import httpx
import pytest

from agentic_sdlc_platform.adapters.hermes import HermesAgentAdapter
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.hermes_session import (
    HermesSessionError,
    HermesSessionRequest,
    HermesStartSessionRequest,
)


async def test_hermes_adapter_blocks_when_http_disabled() -> None:
    adapter = HermesAgentAdapter(Settings(hermes_http_enabled=False))

    with pytest.raises(HermesSessionError, match="hermes HTTP is disabled"):
        await adapter.ask(
            HermesSessionRequest(
                provider="slack",
                channel="C123",
                sender_id="U123",
                text="How does FEFO allocation work?",
            )
        )


async def test_hermes_adapter_posts_direct_question() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            status_code=200,
            json={
                "session_id": "session-1",
                "message_id": "message-1",
                "answer": "FEFO allocates oldest expiring lots first.",
            },
        )

    adapter = HermesAgentAdapter(
        Settings(
            hermes_http_enabled=True,
            hermes_base_url="https://hermes.local",
            hermes_api_key="test-key",
        ),
        transport=httpx.MockTransport(handler),
    )

    response = await adapter.ask(
        HermesSessionRequest(
            provider="slack",
            channel="C123",
            sender_id="U123",
            text="How does FEFO allocation work?",
            repo="keychain-os-erp",
        )
    )

    assert response.session_id == "session-1"
    assert response.message_id == "message-1"
    assert response.answer == "FEFO allocates oldest expiring lots first."
    assert captured_request is not None
    assert str(captured_request.url) == "https://hermes.local/api/sessions/ask"
    assert captured_request.headers["authorization"] == "Bearer test-key"
    assert json.loads(captured_request.content) == {
        "provider": "slack",
        "channel": "C123",
        "sender_id": "U123",
        "text": "How does FEFO allocation work?",
        "repo": "keychain-os-erp",
    }


async def test_hermes_adapter_raises_structured_error_for_failure() -> None:
    adapter = HermesAgentAdapter(
        Settings(
            hermes_http_enabled=True,
            hermes_base_url="https://hermes.local",
            hermes_api_key="test-key",
        ),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status_code=503, json={"error": "offline"})
        ),
    )

    with pytest.raises(HermesSessionError, match="hermes ask failed"):
        await adapter.ask(
            HermesSessionRequest(
                provider="slack",
                channel="C123",
                sender_id="U123",
                text="How does FEFO allocation work?",
            )
        )


async def test_hermes_adapter_starts_task_session() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            status_code=200,
            json={"session_id": "session-1", "message_id": "message-1"},
        )

    adapter = HermesAgentAdapter(
        Settings(
            hermes_http_enabled=True,
            hermes_base_url="https://hermes.local",
            hermes_api_key="test-key",
        ),
        transport=httpx.MockTransport(handler),
    )

    response = await adapter.start_session(
        HermesStartSessionRequest(
            task_id="task-1",
            provider="linear",
            external_thread_id="issue-id-1",
            text="Build webhook bridge",
            repo="keychain-os-erp",
        )
    )

    assert response.session_id == "session-1"
    assert captured_request is not None
    assert str(captured_request.url) == "https://hermes.local/api/sessions"
    assert json.loads(captured_request.content) == {
        "task_id": "task-1",
        "provider": "linear",
        "external_thread_id": "issue-id-1",
        "text": "Build webhook bridge",
        "repo": "keychain-os-erp",
    }


async def test_hermes_adapter_resumes_task_session() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            status_code=200,
            json={"session_id": "session-1", "message_id": "message-2", "answer": "Got it."},
        )

    adapter = HermesAgentAdapter(
        Settings(
            hermes_http_enabled=True,
            hermes_base_url="https://hermes.local",
            hermes_api_key="test-key",
        ),
        transport=httpx.MockTransport(handler),
    )

    response = await adapter.resume_session(
        session_id="session-1",
        text="Please inspect inventory allocation.",
        actor="user-1",
    )

    assert response.message_id == "message-2"
    assert response.answer == "Got it."
    assert captured_request is not None
    assert str(captured_request.url) == "https://hermes.local/api/sessions/session-1/messages"
    assert json.loads(captured_request.content) == {
        "text": "Please inspect inventory allocation.",
        "actor": "user-1",
    }
