import httpx
import pytest

from agentic_sdlc_platform.adapters.graphify import GraphifyGraphStore
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.graph_store import GraphIndexRequest, GraphQuery, GraphStoreError


async def test_graphify_store_blocks_when_disabled() -> None:
    store = GraphifyGraphStore(Settings(vendor_http_enabled=False))

    with pytest.raises(GraphStoreError, match="disabled"):
        await store.query(GraphQuery(repo="repo", question="question"))


async def test_graphify_cli_query_uses_real_graphify_command(tmp_path) -> None:
    graph_path = tmp_path / "graphify-out" / "graph.json"
    graph_path.parent.mkdir()
    graph_path.write_text("{}", encoding="utf-8")
    captured_commands: list[list[str]] = []

    async def runner(command: list[str], command_timeout: float) -> str:
        captured_commands.append(command)
        assert command_timeout == 10.0
        return "Answer from graph.\n- src/forms/foo_dafet.ts:42\n"

    store = GraphifyGraphStore(
        Settings(vendor_http_enabled=True, graphify_command="graphify"),
        runner=runner,
    )

    result = await store.query(
        GraphQuery(
            repo="erp-service",
            question="How does dry run validation work?",
            metadata={"graph_path": str(graph_path)},
        )
    )

    assert captured_commands == [
        [
            "graphify",
            "query",
            "How does dry run validation work?",
            "--graph",
            str(graph_path),
        ]
    ]
    assert result.provider == "graphify"
    assert result.answer == "Answer from graph.\n- src/forms/foo_dafet.ts:42"
    assert result.references == ["src/forms/foo_dafet.ts:42"]


async def test_graphify_cli_index_uses_repo_local_path(tmp_path) -> None:
    repo_path = tmp_path / "repo"
    graph_path = repo_path / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True)
    graph_path.write_text("{}", encoding="utf-8")
    captured_commands: list[list[str]] = []

    async def runner(command: list[str], command_timeout: float) -> str:
        captured_commands.append(command)
        return "indexed"

    store = GraphifyGraphStore(
        Settings(vendor_http_enabled=True, graphify_command="graphify"),
        runner=runner,
    )

    result = await store.index(
        GraphIndexRequest(
            repo="erp-service",
            clone_url="https://github.com/acme-corp/erp-service.git",
            default_branch="main",
            metadata={"local_path": str(repo_path)},
        )
    )

    assert captured_commands == [["graphify", "update", str(repo_path)]]
    assert result.provider == "graphify"
    assert result.external_index_id == str(graph_path)
    assert result.status == "indexed"


async def test_graphify_cli_index_copies_repo_to_output_root(tmp_path) -> None:
    source_repo_path = tmp_path / "host-repo"
    source_repo_path.mkdir()
    (source_repo_path / "app.py").write_text("print('hello')", encoding="utf-8")
    output_root = tmp_path / "graphify-data"
    copied_graph_path = (
        output_root / "acme-corp__erp-service" / "graphify-out" / "graph.json"
    )
    captured_commands: list[list[str]] = []

    async def runner(command: list[str], command_timeout: float) -> str:
        captured_commands.append(command)
        copied_graph_path.parent.mkdir(parents=True)
        copied_graph_path.write_text("{}", encoding="utf-8")
        return "indexed"

    store = GraphifyGraphStore(
        Settings(
            vendor_http_enabled=True,
            graphify_command="graphify",
            graphify_output_root=str(output_root),
        ),
        runner=runner,
    )

    result = await store.index(
        GraphIndexRequest(
            repo="acme-corp/erp-service",
            default_branch="main",
            metadata={"local_path": str(source_repo_path)},
        )
    )

    copied_repo_path = output_root / "acme-corp__erp-service"
    assert (copied_repo_path / "app.py").read_text(encoding="utf-8") == "print('hello')"
    assert captured_commands == [["graphify", "update", str(copied_repo_path)]]
    assert result.external_index_id == str(copied_graph_path)


async def test_graphify_cli_index_ignores_dangling_symlinks(tmp_path) -> None:
    source_repo_path = tmp_path / "host-repo"
    source_repo_path.mkdir()
    (source_repo_path / "app.py").write_text("print('hello')", encoding="utf-8")
    (source_repo_path / "public").mkdir()
    (source_repo_path / "public" / "_next-video").symlink_to(
        source_repo_path / "missing-target"
    )
    output_root = tmp_path / "graphify-data"
    copied_graph_path = output_root / "frontend-monorepo" / "graphify-out" / "graph.json"

    async def runner(command: list[str], command_timeout: float) -> str:
        copied_graph_path.parent.mkdir(parents=True)
        copied_graph_path.write_text("{}", encoding="utf-8")
        return "indexed"

    store = GraphifyGraphStore(
        Settings(
            vendor_http_enabled=True,
            graphify_command="graphify",
            graphify_output_root=str(output_root),
        ),
        runner=runner,
    )

    result = await store.index(
        GraphIndexRequest(
            repo="frontend-monorepo",
            default_branch="main",
            metadata={"local_path": str(source_repo_path)},
        )
    )

    copied_repo_path = output_root / "frontend-monorepo"
    assert (copied_repo_path / "app.py").exists()
    assert not (copied_repo_path / "public" / "_next-video").exists()
    assert result.external_index_id == str(copied_graph_path)


async def test_graphify_cli_requires_graph_path_or_local_repo_path() -> None:
    store = GraphifyGraphStore(Settings(vendor_http_enabled=True))

    with pytest.raises(GraphStoreError, match="graph_path"):
        await store.query(GraphQuery(repo="repo", question="question"))


async def test_graphify_http_query_posts_to_compatible_backend() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            status_code=200,
            json={
                "provider": "graphify",
                "answer": "Inventory allocation lives in inventory/allocation.py.",
                "references": ["inventory/allocation.py"],
            },
        )

    store = GraphifyGraphStore(
        Settings(
            vendor_http_enabled=True,
            graphify_mode="http",
            graphify_base_url="https://graphify.local",
            graphify_api_key="test-key",
        ),
        transport=httpx.MockTransport(handler),
    )

    result = await store.query(
        GraphQuery(
            repo="erp-service",
            question="How does FEFO work?",
            metadata={"default_branch": "main"},
        )
    )

    assert captured_request is not None
    assert str(captured_request.url) == "https://graphify.local/api/query"
    assert captured_request.headers["authorization"] == "Bearer test-key"
    assert result.answer == "Inventory allocation lives in inventory/allocation.py."
    assert result.references == ["inventory/allocation.py"]


async def test_graphify_http_index_posts_to_compatible_backend() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            status_code=200,
            json={
                "external_index_id": "graphify:erp-service:main",
                "status": "indexed",
            },
        )

    store = GraphifyGraphStore(
        Settings(
            vendor_http_enabled=True,
            graphify_mode="http",
            graphify_base_url="https://graphify.local",
        ),
        transport=httpx.MockTransport(handler),
    )

    result = await store.index(
        GraphIndexRequest(
            repo="erp-service",
            clone_url="https://github.com/acme-corp/erp-service.git",
            default_branch="main",
        )
    )

    assert captured_request is not None
    assert str(captured_request.url) == "https://graphify.local/api/index"
    assert result.external_index_id == "graphify:erp-service:main"
    assert result.status == "indexed"
