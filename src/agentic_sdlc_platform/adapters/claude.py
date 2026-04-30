from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.model_provider import (
    ModelProviderError,
    ModelRequest,
)


class ClaudeModelProvider:
    """Claude model provider seam.

    Real HTTP payloads stay out until the concrete Claude API/version and retry policy are chosen.
    """

    provider = "claude"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def complete(self, request: ModelRequest):
        if not self._settings.vendor_http_enabled:
            raise ModelProviderError("vendor HTTP is disabled")

        if not self._settings.claude_api_key:
            raise ModelProviderError("claude API key is not configured")

        raise ModelProviderError(
            "claude model provider is not implemented; configure ASDLC_MODEL_PROVIDER=openai"
        )
