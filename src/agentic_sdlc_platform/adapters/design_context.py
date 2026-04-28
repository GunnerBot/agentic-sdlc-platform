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

    async def fetch(self, url: str) -> DesignContext | None:
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


def build_design_context_adapter(settings: Settings) -> DesignContextPort | None:
    if not settings.figma_http_enabled:
        return None
    return FigmaDesignContextAdapter(settings)


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


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _str_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
