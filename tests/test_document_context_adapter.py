import httpx

from agentic_sdlc_platform.adapters.document_context import (
    GoogleDocsDocumentContextAdapter,
    NotionDocumentContextAdapter,
    google_doc_id,
    notion_page_id,
)
from agentic_sdlc_platform.core.config import Settings


def test_notion_page_id_extracts_slugged_page_id() -> None:
    assert notion_page_id(
        "https://acme.notion.site/Dynamic-form-titles-"
        "1234567890abcdef1234567890abcdef?pvs=4"
    ) == "1234567890abcdef1234567890abcdef"


def test_google_doc_id_extracts_document_id() -> None:
    assert google_doc_id("https://docs.google.com/document/d/doc-123/edit") == "doc-123"


async def test_notion_adapter_fetches_page_title_and_block_text() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/children"):
            return httpx.Response(
                status_code=200,
                json={
                    "results": [
                        {
                            "type": "heading_2",
                            "heading_2": {
                                "rich_text": [{"plain_text": "Repositories"}]
                            },
                        },
                        {
                            "type": "bulleted_list_item",
                            "bulleted_list_item": {
                                "rich_text": [{"plain_text": "keychain-os-erp"}]
                            },
                        },
                    ]
                },
            )
        return httpx.Response(
            status_code=200,
            json={
                "properties": {
                    "title": {
                        "title": [{"plain_text": "Dynamic form titles"}]
                    }
                }
            },
        )

    adapter = NotionDocumentContextAdapter(
        Settings(
            notion_http_enabled=True,
            notion_api_key="notion-secret",
        ),
        transport=httpx.MockTransport(handler),
    )

    context = await adapter.fetch(
        "https://acme.notion.site/Dynamic-form-titles-1234567890abcdef1234567890abcdef"
    )

    assert context is not None
    assert context.provider == "notion"
    assert context.title == "Dynamic form titles"
    assert context.text == "Repositories\nkeychain-os-erp"
    assert requests[0].headers["authorization"] == "Bearer notion-secret"
    assert requests[0].headers["notion-version"] == "2022-06-28"


async def test_google_docs_adapter_fetches_exported_text() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(status_code=200, text="## Repositories\n- webapp-monorepo")

    adapter = GoogleDocsDocumentContextAdapter(
        Settings(
            google_docs_http_enabled=True,
            google_docs_bearer_token="google-token",
        ),
        transport=httpx.MockTransport(handler),
    )

    context = await adapter.fetch("https://docs.google.com/document/d/doc-123/edit")

    assert context is not None
    assert context.provider == "google_docs"
    assert context.text == "## Repositories\n- webapp-monorepo"
    assert captured_request is not None
    assert captured_request.headers["authorization"] == "Bearer google-token"
    assert captured_request.url.path == "/document/d/doc-123/export"
