import json

import httpx

from agentic_sdlc_platform.adapters.multica_workspace import MulticaWorkspaceRepoRegistry
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.runtime_repo_registry import (
    RuntimeRepoRegistryError,
    RuntimeRepository,
)


async def test_multica_workspace_repo_registry_replaces_checkout_urls() -> None:
    patched_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer multica-pat"
        if request.method == "PATCH" and request.url.path == "/api/workspaces/ws-1":
            payload = json.loads(request.content.decode())
            patched_payloads.append(payload)
            return httpx.Response(200, json={"id": "ws-1", "repos": payload["repos"]})
        return httpx.Response(404, json={"error": "not found"})

    registry = MulticaWorkspaceRepoRegistry(
        Settings(
            _env_file=None,
            multica_http_enabled=True,
            multica_base_url="http://multica.test",
            multica_api_key="multica-pat",
            multica_workspace_id="ws-1",
        ),
        transport=httpx.MockTransport(handler),
    )

    result = await registry.sync_repositories(
        [
            RuntimeRepository(
                name="acme/service",
                provider="github",
                clone_url="https://github.com/acme/service.git",
                description="acme/service",
                metadata={"github_html_url": "https://github.com/acme/service"},
            )
        ]
    )

    assert patched_payloads == [
        {
            "repos": [
                {
                    "url": "https://github.com/acme/service.git",
                    "description": "acme/service",
                },
                {
                    "url": "https://github.com/acme/service",
                    "description": "acme/service",
                },
            ]
        }
    ]
    assert result.workspace_id == "ws-1"
    assert result.repo_count == 2
    assert result.urls == (
        "https://github.com/acme/service",
        "https://github.com/acme/service.git",
    )


async def test_multica_workspace_repo_registry_prunes_to_empty_repo_list() -> None:
    patched_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH" and request.url.path == "/api/workspaces/ws-1":
            payload = json.loads(request.content.decode())
            patched_payloads.append(payload)
            return httpx.Response(200, json={"id": "ws-1", "repos": []})
        return httpx.Response(404, json={"error": "not found"})

    registry = MulticaWorkspaceRepoRegistry(
        Settings(
            _env_file=None,
            multica_http_enabled=True,
            multica_base_url="http://multica.test",
            multica_api_key="multica-pat",
            multica_workspace_id="ws-1",
        ),
        transport=httpx.MockTransport(handler),
    )

    result = await registry.sync_repositories([])

    assert patched_payloads == [{"repos": []}]
    assert result.repo_count == 0
    assert result.urls == ()


async def test_multica_workspace_repo_registry_rejects_extra_persisted_repos() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH" and request.url.path == "/api/workspaces/ws-1":
            payload = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "id": "ws-1",
                    "repos": [
                        *payload["repos"],
                        {
                            "url": "https://github.com/acme/stale",
                            "description": "stale",
                        },
                    ],
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    registry = MulticaWorkspaceRepoRegistry(
        Settings(
            _env_file=None,
            multica_http_enabled=True,
            multica_base_url="http://multica.test",
            multica_api_key="multica-pat",
            multica_workspace_id="ws-1",
        ),
        transport=httpx.MockTransport(handler),
    )

    try:
        await registry.sync_repositories(
            [
                RuntimeRepository(
                    name="acme/service",
                    provider="github",
                    clone_url="https://github.com/acme/service.git",
                )
            ]
        )
    except RuntimeRepoRegistryError as exc:
        assert "retained unexpected repos" in str(exc)
    else:
        raise AssertionError("expected Multica sync to reject extra persisted repos")
