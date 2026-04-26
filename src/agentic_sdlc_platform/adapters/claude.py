from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.model_provider import (
    ModelProviderError,
    ModelRequest,
    ModelResponse,
)


class ClaudeModelProvider:
    """Claude model provider seam.

    Real HTTP payloads stay out until the concrete Claude API/version and retry policy are chosen.
    """

    provider = "claude"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._settings.vendor_http_enabled:
            raise ModelProviderError("vendor HTTP is disabled")

        if not self._settings.claude_api_key:
            raise ModelProviderError("claude API key is not configured")

        return ModelResponse(
            provider=self.provider,
            model=self._settings.claude_default_model or "claude-default",
            content=f"claude provider accepted role={request.role}",
            request_id=request.task_id,
        )
