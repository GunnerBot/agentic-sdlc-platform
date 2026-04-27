import asyncio
from collections.abc import Awaitable, Callable

import httpx

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.model_provider import (
    ModelProviderError,
    ModelRequest,
    ModelResponse,
)


class OpenAIModelProvider:
    provider = "openai"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._sleep = sleep

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._settings.vendor_http_enabled:
            raise ModelProviderError("vendor HTTP is disabled")
        if not self._settings.openai_api_key:
            raise ModelProviderError("openai API key is not configured")

        model = request.metadata.get("model") or self._settings.openai_default_model
        response = await self._request_with_retries(
            {
                "model": model,
                "input": [
                    {
                        "role": "system",
                        "content": f"You are acting as {request.role}.",
                    },
                    {
                        "role": "user",
                        "content": request.prompt,
                    },
                ],
            }
        )
        payload = response.json()
        return ModelResponse(
            provider=self.provider,
            model=model,
            content=_extract_text(payload),
            request_id=_str(payload.get("id")) or request.task_id,
        )

    async def _request_with_retries(self, payload: dict[str, object]) -> httpx.Response:
        attempts = self._settings.openai_max_retries + 1
        try:
            async with httpx.AsyncClient(
                base_url=self._settings.openai_base_url,
                timeout=self._settings.openai_timeout_seconds,
                transport=self._transport,
            ) as client:
                for attempt in range(attempts):
                    response = await client.post(
                        "/responses",
                        json=payload,
                        headers={"Authorization": f"Bearer {self._settings.openai_api_key}"},
                    )
                    if response.status_code < 500:
                        response.raise_for_status()
                        return response
                    if attempt == attempts - 1:
                        response.raise_for_status()
                    await self._sleep(0.25 * (2**attempt))
        except httpx.HTTPError as exc:
            raise ModelProviderError("openai completion failed") from exc
        raise ModelProviderError("openai completion failed")


def _extract_text(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ModelProviderError("openai response was not an object")
    output_text = payload.get("output_text")
    if isinstance(output_text, str):
        return output_text
    output = payload.get("output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for content_item in content:
                if isinstance(content_item, dict) and isinstance(content_item.get("text"), str):
                    parts.append(content_item["text"])
        if parts:
            return "".join(parts)
    raise ModelProviderError("openai response did not include output text")


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
