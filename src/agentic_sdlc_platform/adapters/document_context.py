import re
from urllib.parse import parse_qs, urlparse

import httpx

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.document_context import (
    DocumentContext,
    DocumentContextError,
    DocumentContextPort,
)

NOTION_HOST_RE = re.compile(r"(^|\.)notion\.(site|so)$", re.IGNORECASE)
NOTION_PAGE_ID_RE = re.compile(r"([0-9a-f]{32}|[0-9a-f-]{36})(?:[?#].*)?$", re.IGNORECASE)
GOOGLE_DOCS_HOSTS = {"docs.google.com"}


class CompositeDocumentContextAdapter:
    def __init__(self, adapters: list[DocumentContextPort]) -> None:
        self._adapters = adapters

    async def fetch(self, url: str) -> DocumentContext | None:
        for adapter in self._adapters:
            context = await adapter.fetch(url)
            if context is not None:
                return context
        return None


class NotionDocumentContextAdapter:
    provider = "notion"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def fetch(self, url: str) -> DocumentContext | None:
        page_id = notion_page_id(url)
        if page_id is None:
            return None
        if not self._settings.notion_http_enabled:
            return None
        if not self._settings.notion_api_key:
            raise DocumentContextError("notion API key is not configured")

        headers = {
            "Authorization": f"Bearer {self._settings.notion_api_key}",
            "Notion-Version": self._settings.notion_version,
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._settings.notion_base_url,
                timeout=self._settings.notion_timeout_seconds,
                transport=self._transport,
            ) as client:
                page_response = await client.get(f"/v1/pages/{page_id}", headers=headers)
                page_response.raise_for_status()
                blocks_response = await client.get(
                    f"/v1/blocks/{page_id}/children",
                    params={"page_size": 100},
                    headers=headers,
                )
                blocks_response.raise_for_status()
        except httpx.HTTPError as exc:
            raise DocumentContextError("notion document fetch failed") from exc

        page = page_response.json()
        blocks = blocks_response.json()
        return DocumentContext(
            provider=self.provider,
            url=url,
            title=_notion_title(page),
            text=_notion_blocks_text(blocks),
            metadata={"page_id": page_id},
        )


class GoogleDocsDocumentContextAdapter:
    provider = "google_docs"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def fetch(self, url: str) -> DocumentContext | None:
        doc_id = google_doc_id(url)
        if doc_id is None:
            return None
        if not self._settings.google_docs_http_enabled:
            return None

        headers = {}
        if self._settings.google_docs_bearer_token:
            headers["Authorization"] = f"Bearer {self._settings.google_docs_bearer_token}"
        try:
            async with httpx.AsyncClient(
                base_url=self._settings.google_docs_base_url,
                timeout=self._settings.google_docs_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    f"/document/d/{doc_id}/export",
                    params={"format": "txt"},
                    headers=headers,
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise DocumentContextError("google docs document fetch failed") from exc

        return DocumentContext(
            provider=self.provider,
            url=url,
            title=f"Google Doc {doc_id}",
            text=response.text,
            metadata={"doc_id": doc_id},
        )


def build_document_context_adapter(
    settings: Settings,
) -> DocumentContextPort | None:
    adapters: list[DocumentContextPort] = [
        NotionDocumentContextAdapter(settings),
        GoogleDocsDocumentContextAdapter(settings),
    ]
    if not settings.notion_http_enabled and not settings.google_docs_http_enabled:
        return None
    return CompositeDocumentContextAdapter(adapters)


def notion_page_id(url: str) -> str | None:
    parsed = urlparse(url)
    if not parsed.netloc or NOTION_HOST_RE.search(parsed.netloc) is None:
        return None
    path = parsed.path.rstrip("/")
    if not path:
        return None
    match = NOTION_PAGE_ID_RE.search(path.split("/")[-1])
    if not match:
        return None
    return match.group(1).replace("-", "")


def google_doc_id(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc not in GOOGLE_DOCS_HOSTS:
        return None
    path_parts = [part for part in parsed.path.split("/") if part]
    if "document" in path_parts and "d" in path_parts:
        index = path_parts.index("d")
        if index + 1 < len(path_parts):
            return path_parts[index + 1]
    query_doc_id = parse_qs(parsed.query).get("id")
    if query_doc_id:
        return query_doc_id[0]
    return None


def _notion_title(page: object) -> str | None:
    if not isinstance(page, dict):
        return None
    properties = page.get("properties")
    if not isinstance(properties, dict):
        return None
    for property_value in properties.values():
        if not isinstance(property_value, dict):
            continue
        title = property_value.get("title")
        if not isinstance(title, list):
            continue
        text = _rich_text_plain_text(title)
        if text:
            return text
    return None


def _notion_blocks_text(blocks: object) -> str:
    if not isinstance(blocks, dict):
        return ""
    results = blocks.get("results")
    if not isinstance(results, list):
        return ""
    lines = []
    for block in results:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if not isinstance(block_type, str):
            continue
        block_value = block.get(block_type)
        if not isinstance(block_value, dict):
            continue
        rich_text = block_value.get("rich_text")
        if isinstance(rich_text, list):
            text = _rich_text_plain_text(rich_text)
            if text:
                lines.append(text)
    return "\n".join(lines)


def _rich_text_plain_text(values: list[object]) -> str:
    parts = []
    for value in values:
        if isinstance(value, dict) and isinstance(value.get("plain_text"), str):
            parts.append(value["plain_text"])
    return "".join(parts).strip()
