import json

import httpx
import pytest

from agentic_sdlc_platform.adapters.openai import OpenAIModelProvider
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.model_provider import ModelProviderError, ModelRequest


async def test_openai_provider_blocks_when_vendor_http_disabled() -> None:
    provider = OpenAIModelProvider(Settings(vendor_http_enabled=False))

    with pytest.raises(ModelProviderError, match="vendor HTTP is disabled"):
        await provider.complete(ModelRequest(role="plan_agent", prompt="Plan this"))


async def test_openai_provider_requires_api_key_when_enabled() -> None:
    provider = OpenAIModelProvider(
        Settings(vendor_http_enabled=True, openai_api_key="")
    )

    with pytest.raises(ModelProviderError, match="openai API key"):
        await provider.complete(ModelRequest(role="plan_agent", prompt="Plan this"))


async def test_openai_provider_posts_responses_request() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            status_code=200,
            json={"id": "resp-1", "output_text": "[]"},
        )

    provider = OpenAIModelProvider(
        Settings(
            vendor_http_enabled=True,
            openai_api_key="test-key",
            openai_default_model="gpt-5.5",
        ),
        transport=httpx.MockTransport(handler),
    )

    response = await provider.complete(
        ModelRequest(
            role="plan_agent",
            prompt="Plan this",
            task_id="task-1",
        )
    )

    assert response.provider == "openai"
    assert response.model == "gpt-5.5"
    assert response.content == "[]"
    assert response.request_id == "resp-1"
    assert captured_request is not None
    assert str(captured_request.url) == "https://api.openai.com/v1/responses"
    assert captured_request.headers["authorization"] == "Bearer test-key"
    assert json.loads(captured_request.content) == {
        "model": "gpt-5.5",
        "input": [
            {"role": "system", "content": "You are acting as plan_agent."},
            {"role": "user", "content": "Plan this"},
        ],
    }


async def test_openai_provider_allows_per_request_model_override() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["model"] == "gpt-5.4-mini"
        return httpx.Response(
            status_code=200,
            json={
                "id": "resp-1",
                "output": [{"content": [{"text": "fallback ok"}]}],
            },
        )

    provider = OpenAIModelProvider(
        Settings(vendor_http_enabled=True, openai_api_key="test-key"),
        transport=httpx.MockTransport(handler),
    )

    response = await provider.complete(
        ModelRequest(
            role="plan_agent",
            prompt="Plan this",
            metadata={"model": "gpt-5.4-mini"},
        )
    )

    assert response.model == "gpt-5.4-mini"
    assert response.content == "fallback ok"
