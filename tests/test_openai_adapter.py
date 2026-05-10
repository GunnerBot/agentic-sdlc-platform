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
    provider = OpenAIModelProvider(Settings(vendor_http_enabled=True, openai_api_key=""))

    with pytest.raises(ModelProviderError, match="openai API key"):
        await provider.complete(ModelRequest(role="plan_agent", prompt="Plan this"))


async def test_openai_provider_posts_responses_request() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            status_code=200,
            json={
                "id": "resp-1",
                "output_text": "[]",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "total_tokens": 12,
                },
            },
        )

    provider = OpenAIModelProvider(
        Settings(
            vendor_http_enabled=True,
            openai_api_key="test-key",
            openai_planner_model="gpt-5-mini",
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
    assert response.model == "gpt-5-mini"
    assert response.content == "[]"
    assert response.request_id == "resp-1"
    assert response.usage is not None
    assert response.usage["input_tokens"] == 10
    assert response.usage["output_tokens"] == 2
    assert response.usage["total_tokens"] == 12
    assert response.usage["estimation_method"] == "provider_usage"
    assert captured_request is not None
    assert str(captured_request.url) == "https://api.openai.com/v1/responses"
    assert captured_request.headers["authorization"] == "Bearer test-key"
    assert json.loads(captured_request.content) == {
        "model": "gpt-5-mini",
        "input": [
            {"role": "system", "content": "You are acting as plan_agent."},
            {"role": "user", "content": "Plan this"},
        ],
    }


async def test_openai_provider_routes_roles_to_budgeted_models() -> None:
    captured_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_models.append(json.loads(request.content)["model"])
        return httpx.Response(status_code=200, json={"id": "resp-1", "output_text": "{}"})

    provider = OpenAIModelProvider(
        Settings(vendor_http_enabled=True, openai_api_key="test-key"),
        transport=httpx.MockTransport(handler),
    )

    await provider.complete(ModelRequest(role="router_agent", prompt="Classify"))
    await provider.complete(ModelRequest(role="plan_agent", prompt="Plan"))
    await provider.complete(ModelRequest(role="planner_escalation_agent", prompt="Retry"))
    await provider.complete(ModelRequest(role="premium_escalation_agent", prompt="Hard"))

    assert captured_models == [
        "gpt-5-nano",
        "gpt-5.5",
        "gpt-5.5",
        "gpt-5.5",
    ]


async def test_openai_provider_allows_per_request_model_override() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["model"] == "gpt-5"
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
            metadata={"model": "gpt-5"},
        )
    )

    assert response.model == "gpt-5"
    assert response.content == "fallback ok"
