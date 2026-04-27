import pytest

from agentic_sdlc_platform.adapters.graphify import GraphifyGraphStore
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.graph_store import GraphIndexRequest, GraphQuery, GraphStoreError


async def test_graphify_store_blocks_when_vendor_http_disabled() -> None:
    store = GraphifyGraphStore(Settings(vendor_http_enabled=False))

    with pytest.raises(GraphStoreError, match="vendor HTTP is disabled"):
        await store.query(GraphQuery(repo="repo", question="question"))


async def test_graphify_store_requires_base_url_when_enabled() -> None:
    store = GraphifyGraphStore(Settings(vendor_http_enabled=True))

    with pytest.raises(GraphStoreError, match="base URL"):
        await store.query(GraphQuery(repo="repo", question="question"))


async def test_graphify_store_returns_internal_result_shape_when_configured() -> None:
    store = GraphifyGraphStore(
        Settings(vendor_http_enabled=True, graphify_base_url="https://graphify.local")
    )

    result = await store.query(GraphQuery(repo="repo", question="question"))

    assert result.provider == "graphify"
    assert result.references == ["https://graphify.local"]


async def test_graphify_store_accepts_repo_index_request_when_configured() -> None:
    store = GraphifyGraphStore(
        Settings(vendor_http_enabled=True, graphify_base_url="https://graphify.local")
    )

    result = await store.index(
        GraphIndexRequest(
            repo="keychain-os-erp",
            clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
            default_branch="main",
        )
    )

    assert result.provider == "graphify"
    assert result.external_index_id == "graphify:keychain-os-erp:main"
    assert result.status == "indexed"
