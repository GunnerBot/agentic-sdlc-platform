import httpx

from agentic_sdlc_platform.adapters.design_context import (
    FigmaDesignContextAdapter,
    OpenAIImageDesignContextAdapter,
    figma_reference,
)
from agentic_sdlc_platform.core.config import Settings


def test_figma_reference_extracts_file_and_node_id() -> None:
    reference = figma_reference("https://www.figma.com/design/abc123/Form-title-flow?node-id=1%3A2")

    assert reference is not None
    assert reference.file_key == "abc123"
    assert reference.node_id == "1:2"


async def test_figma_adapter_fetches_file_metadata() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            status_code=200,
            json={
                "name": "Dynamic form titles",
                "lastModified": "2026-04-27T12:00:00Z",
                "thumbnailUrl": "https://figma.local/thumb.png",
                "nodes": {
                    "1:2": {
                        "document": {
                            "name": "Title field frame",
                            "type": "FRAME",
                            "children": [{"name": "Title input", "type": "TEXT"}],
                        }
                    }
                },
            },
        )

    adapter = FigmaDesignContextAdapter(
        Settings(
            figma_http_enabled=True,
            figma_api_key="figma-token",
        ),
        transport=httpx.MockTransport(handler),
    )

    context = await adapter.fetch("https://www.figma.com/file/abc123/Form-title-flow?node-id=1%3A2")

    assert context is not None
    assert context.provider == "figma"
    assert context.title == "Dynamic form titles / Title field frame"
    assert "Figma file: Dynamic form titles" in context.summary
    assert "Requested node: 1:2" in context.summary
    assert "Title field frame (FRAME, children=1)" in context.summary
    assert context.metadata == {
        "file_key": "abc123",
        "node_id": "1:2",
        "last_modified": "2026-04-27T12:00:00Z",
        "thumbnail_url": "https://figma.local/thumb.png",
    }
    assert captured_request is not None
    assert captured_request.url.path == "/v1/files/abc123"
    assert captured_request.url.params["ids"] == "1:2"
    assert captured_request.headers["x-figma-token"] == "figma-token"


async def test_openai_image_adapter_fetches_and_summarizes_attachment() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "linear.app":
            return httpx.Response(
                status_code=200,
                headers={"content-type": "image/png"},
                content=b"fake-png-bytes",
            )
        return httpx.Response(
            status_code=200,
            json={
                "id": "resp-1",
                "output_text": (
                    "Image shows a compact dynamic form title above the fields. "
                    "Repository: frontend-monorepo."
                ),
            },
        )

    adapter = OpenAIImageDesignContextAdapter(
        Settings(
            vendor_http_enabled=True,
            openai_api_key="openai-token",
            linear_api_key="linear-token",
            design_image_hydration_enabled=True,
            design_image_summary_model="gpt-5",
        ),
        transport=httpx.MockTransport(handler),
    )

    context = await adapter.fetch(
        "https://linear.app/attachments/form-title.png",
        title="form-title.png",
        content_type="image/png",
    )

    assert context is not None
    assert context.provider == "openai_vision"
    assert context.title == "form-title.png"
    assert "Repository: frontend-monorepo" in context.summary
    assert context.metadata == {
        "source_content_type": "image/png",
        "byte_count": len(b"fake-png-bytes"),
        "summary_provider": "openai",
        "summary_model": "gpt-5",
    }
    assert requests[0].url == "https://linear.app/attachments/form-title.png"
    assert requests[0].headers["authorization"] == "Bearer linear-token"
    assert requests[1].url.path.endswith("/responses")
    assert requests[1].headers["authorization"] == "Bearer openai-token"
    payload = requests[1].read()
    assert b"fake-png-bytes" not in payload
    assert b"data:image/png;base64" in payload
