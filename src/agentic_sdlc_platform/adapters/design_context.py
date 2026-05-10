import base64
import mimetypes
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.design_context import (
    DesignContext,
    DesignContextError,
    DesignContextPort,
)


@dataclass(frozen=True)
class FigmaReference:
    file_key: str
    node_id: str | None = None


class FigmaDesignContextAdapter:
    provider = "figma"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def fetch(
        self,
        url: str,
        *,
        title: str | None = None,
        content_type: str | None = None,
    ) -> DesignContext | None:
        del title, content_type
        reference = figma_reference(url)
        if reference is None:
            return None
        if not self._settings.figma_http_enabled:
            return None
        if not self._settings.figma_api_key:
            raise DesignContextError("figma API key is not configured")

        try:
            async with httpx.AsyncClient(
                base_url=self._settings.figma_base_url,
                timeout=self._settings.figma_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    f"/v1/files/{reference.file_key}",
                    params={"ids": reference.node_id} if reference.node_id else None,
                    headers={"X-Figma-Token": self._settings.figma_api_key},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise DesignContextError("figma design fetch failed") from exc

        payload = response.json()
        return DesignContext(
            provider=self.provider,
            url=url,
            title=_figma_title(payload, reference),
            summary=_figma_summary(payload, reference),
            metadata={
                "file_key": reference.file_key,
                "node_id": reference.node_id,
                "last_modified": _str_value(_dict_value(payload).get("lastModified")),
                "thumbnail_url": _str_value(_dict_value(payload).get("thumbnailUrl")),
            },
        )


class OpenAIImageDesignContextAdapter:
    provider = "openai_vision"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def fetch(
        self,
        url: str,
        *,
        title: str | None = None,
        content_type: str | None = None,
    ) -> DesignContext | None:
        if not self._is_image_reference(url=url, title=title, content_type=content_type):
            return None
        if not self._settings.design_image_hydration_enabled:
            return None
        if self._settings.design_image_summary_provider != "openai":
            raise DesignContextError("unsupported image summary provider")
        if not self._settings.vendor_http_enabled:
            raise DesignContextError("vendor HTTP is disabled")
        if not self._settings.openai_api_key:
            raise DesignContextError("openai API key is not configured")

        image_bytes, resolved_content_type = await self._fetch_image(url, content_type)
        model = (
            self._settings.design_image_summary_model
            or self._settings.openai_fallback_model
            or self._settings.openai_default_model
        )
        summary = await self._summarize_image(
            image_bytes=image_bytes,
            content_type=resolved_content_type,
            model=model,
            title=title,
            url=url,
        )
        return DesignContext(
            provider=self.provider,
            url=url,
            title=title or _filename_from_url(url) or "Design image",
            summary=summary,
            metadata={
                "source_content_type": resolved_content_type,
                "byte_count": len(image_bytes),
                "summary_provider": "openai",
                "summary_model": model,
            },
        )

    async def _fetch_image(
        self,
        url: str,
        provided_content_type: str | None,
    ) -> tuple[bytes, str]:
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.design_image_fetch_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(url, headers=_image_fetch_headers(url, self._settings))
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise DesignContextError("image design fetch failed") from exc

        image_bytes = response.content
        if len(image_bytes) > self._settings.design_image_max_bytes:
            raise DesignContextError("image design exceeds configured size limit")
        content_type = _response_content_type(response) or provided_content_type
        if not content_type or not content_type.lower().startswith("image/"):
            raise DesignContextError("image design response was not an image")
        return image_bytes, content_type

    async def _summarize_image(
        self,
        *,
        image_bytes: bytes,
        content_type: str,
        model: str,
        title: str | None,
        url: str,
    ) -> str:
        image_url = f"data:{content_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        prompt = "\n".join(
            [
                "Summarize this Linear design/image attachment for an engineering agent.",
                (
                    "Focus on visible UI requirements, states, labels, layout, errors, "
                    "and acceptance-relevant details."
                ),
                "If repository names or implementation hints are visible, include them exactly.",
                f"Attachment title: {title or 'unknown'}",
                f"Source URL: {url}",
            ]
        )
        payload: dict[str, object] = {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_url},
                    ],
                }
            ],
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._settings.openai_base_url,
                timeout=self._settings.openai_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    "/responses",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._settings.openai_api_key}"},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise DesignContextError("image design summarization failed") from exc
        return _extract_openai_text(response.json())

    def _is_image_reference(
        self,
        *,
        url: str,
        title: str | None,
        content_type: str | None,
    ) -> bool:
        if content_type and content_type.lower().startswith("image/"):
            return True
        guessed_type = mimetypes.guess_type(title or url)[0]
        return bool(guessed_type and guessed_type.startswith("image/"))


class CompositeDesignContextAdapter:
    def __init__(self, adapters: list[DesignContextPort]) -> None:
        self._adapters = adapters

    async def fetch(
        self,
        url: str,
        *,
        title: str | None = None,
        content_type: str | None = None,
    ) -> DesignContext | None:
        for adapter in self._adapters:
            context = await adapter.fetch(url, title=title, content_type=content_type)
            if context is not None:
                return context
        return None


def build_design_context_adapter(settings: Settings) -> DesignContextPort | None:
    adapters: list[DesignContextPort] = []
    if settings.figma_http_enabled:
        adapters.append(FigmaDesignContextAdapter(settings))
    if settings.design_image_hydration_enabled:
        adapters.append(OpenAIImageDesignContextAdapter(settings))
    if not adapters:
        return None
    if len(adapters) == 1:
        return adapters[0]
    return CompositeDesignContextAdapter(adapters)


def figma_reference(url: str) -> FigmaReference | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host not in {"figma.com", "www.figma.com"}:
        return None
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 2 or path_parts[0] not in {"file", "design", "proto"}:
        return None
    file_key = path_parts[1]
    query = parse_qs(parsed.query)
    node_id = query.get("node-id", [None])[0]
    if node_id:
        node_id = unquote(node_id)
    return FigmaReference(file_key=file_key, node_id=node_id)


def _figma_title(payload: object, reference: FigmaReference) -> str:
    value = _dict_value(payload)
    name = _str_value(value.get("name"))
    if reference.node_id:
        node_name = _node_name(value, reference.node_id)
        if node_name:
            return f"{name or 'Figma file'} / {node_name}"
    return name or f"Figma file {reference.file_key}"


def _figma_summary(payload: object, reference: FigmaReference) -> str:
    value = _dict_value(payload)
    lines = [f"Figma file: {_str_value(value.get('name')) or reference.file_key}"]
    if reference.node_id:
        lines.append(f"Requested node: {reference.node_id}")
    last_modified = _str_value(value.get("lastModified"))
    if last_modified:
        lines.append(f"Last modified: {last_modified}")
    node_summaries = _node_summaries(value, reference.node_id)
    if node_summaries:
        lines.append("Relevant nodes:")
        lines.extend(f"- {summary}" for summary in node_summaries[:12])
    return "\n".join(lines)


def _node_summaries(payload: dict[str, object], node_id: str | None) -> list[str]:
    if node_id:
        nodes = _dict_value(payload.get("nodes"))
        node_wrapper = _dict_value(nodes.get(node_id))
        document = _dict_value(node_wrapper.get("document"))
        if document:
            return [_node_summary(document)]

    document = _dict_value(payload.get("document"))
    children = document.get("children")
    if not isinstance(children, list):
        return []
    summaries = []
    for child in children:
        if isinstance(child, dict):
            summaries.append(_node_summary(child))
    return [summary for summary in summaries if summary]


def _node_name(payload: dict[str, object], node_id: str) -> str | None:
    nodes = _dict_value(payload.get("nodes"))
    node_wrapper = _dict_value(nodes.get(node_id))
    document = _dict_value(node_wrapper.get("document"))
    return _str_value(document.get("name"))


def _node_summary(node: dict[str, object]) -> str:
    name = _str_value(node.get("name")) or "Unnamed"
    node_type = _str_value(node.get("type")) or "UNKNOWN"
    children = node.get("children")
    child_count = len(children) if isinstance(children, list) else 0
    return f"{name} ({node_type}, children={child_count})"


def _image_fetch_headers(url: str, settings: Settings) -> dict[str, str]:
    parsed = urlparse(url)
    if settings.linear_api_key and parsed.netloc.lower().endswith("linear.app"):
        return {"Authorization": f"Bearer {settings.linear_api_key}"}
    return {}


def _response_content_type(response: httpx.Response) -> str | None:
    content_type = response.headers.get("content-type")
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower()


def _filename_from_url(url: str) -> str | None:
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    filename = path.rsplit("/", 1)[-1]
    return unquote(filename) if filename else None


def _extract_openai_text(payload: object) -> str:
    if not isinstance(payload, dict):
        raise DesignContextError("openai response was not an object")
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text:
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
    raise DesignContextError("openai response did not include output text")


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _str_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
