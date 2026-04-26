import pytest

from agentic_sdlc_platform.adapters.claude import ClaudeModelProvider
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.model_provider import ModelProviderError, ModelRequest


async def test_claude_provider_blocks_when_vendor_http_disabled() -> None:
    provider = ClaudeModelProvider(Settings(vendor_http_enabled=False))

    with pytest.raises(ModelProviderError, match="vendor HTTP is disabled"):
        await provider.complete(ModelRequest(role="critic_agent", prompt="review"))


async def test_claude_provider_requires_api_key_when_enabled() -> None:
    provider = ClaudeModelProvider(Settings(vendor_http_enabled=True))

    with pytest.raises(ModelProviderError, match="API key"):
        await provider.complete(ModelRequest(role="critic_agent", prompt="review"))


async def test_claude_provider_returns_internal_response_shape_when_configured() -> None:
    provider = ClaudeModelProvider(
        Settings(
            vendor_http_enabled=True,
            claude_api_key="test-key",
            claude_default_model="claude-test",
        )
    )

    response = await provider.complete(
        ModelRequest(role="critic_agent", prompt="review", task_id="task-1")
    )

    assert response.provider == "claude"
    assert response.model == "claude-test"
    assert response.request_id == "task-1"
