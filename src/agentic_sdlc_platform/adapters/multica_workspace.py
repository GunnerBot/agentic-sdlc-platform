import asyncio
from collections import OrderedDict
from collections.abc import Sequence

import httpx

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.runtime_repo_registry import (
    RuntimeRepoRegistryError,
    RuntimeRepoRegistryPort,
    RuntimeRepository,
    RuntimeRepoSyncResult,
)


class MulticaWorkspaceRepoRegistry(RuntimeRepoRegistryPort):
    provider = "multica"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._sync_lock = asyncio.Lock()

    async def sync_repositories(
        self,
        repositories: Sequence[RuntimeRepository],
    ) -> RuntimeRepoSyncResult:
        base_url = self._settings.multica_base_url
        api_key = self._settings.multica_api_key
        workspace_id = self._settings.multica_workspace_id
        if not base_url or not api_key or not workspace_id:
            raise RuntimeRepoRegistryError(
                "Multica repository sync requires base URL, API key, and workspace ID"
            )

        required_repos = _workspace_repo_entries(repositories)
        async with httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=self._settings.multica_timeout_seconds,
            transport=self._transport,
        ) as client:
            async with self._sync_lock:
                updated = await self._request(
                    client,
                    "PATCH",
                    f"/api/workspaces/{workspace_id}",
                    json={"repos": required_repos},
                    failure_message="multica workspace repository sync failed",
                )

        synced_urls = {
            item["url"]
            for item in _json_list(updated.get("repos"), "updated Multica workspace repos")
            if isinstance(item.get("url"), str)
        }
        required_canonical_urls = {
            _canonical_repo_url(item["url"]) for item in required_repos
        }
        synced_canonical_urls = {_canonical_repo_url(url) for url in synced_urls}
        missing_urls = sorted(required_canonical_urls - synced_canonical_urls)
        if missing_urls:
            raise RuntimeRepoRegistryError(
                "Multica workspace repository sync did not persist required repos: "
                + ", ".join(missing_urls)
            )
        extra_urls = sorted(synced_canonical_urls - required_canonical_urls)
        if extra_urls:
            raise RuntimeRepoRegistryError(
                "Multica workspace repository sync retained unexpected repos: "
                + ", ".join(extra_urls)
            )

        return RuntimeRepoSyncResult(
            provider=self.provider,
            workspace_id=workspace_id,
            repo_count=len(synced_urls),
            urls=tuple(sorted(synced_urls)),
        )

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        failure_message: str,
        json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        try:
            response = await client.request(method, path, json=json)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeRepoRegistryError(
                f"{failure_message}: HTTP {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeRepoRegistryError(f"{failure_message}: {exc}") from exc

        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeRepoRegistryError(f"{failure_message}: expected object response")
        return payload


def _workspace_repo_entries(
    repositories: Sequence[RuntimeRepository],
) -> list[dict[str, str]]:
    entries: OrderedDict[str, dict[str, str]] = OrderedDict()
    for repo in repositories:
        description = repo.description or repo.name
        for url in _checkout_url_variants(repo):
            entries[url] = {"url": url, "description": description}
    return list(entries.values())


def _checkout_url_variants(repo: RuntimeRepository) -> list[str]:
    urls: list[str] = []
    metadata = repo.metadata or {}
    for raw_url in (
        repo.clone_url,
        metadata.get("github_html_url"),
        _github_https_url(repo),
    ):
        if isinstance(raw_url, str):
            _append_url_variant(urls, raw_url)
    return urls


def _github_https_url(repo: RuntimeRepository) -> str | None:
    if repo.provider != "github" or "/" not in repo.name:
        return None
    return f"https://github.com/{repo.name}"


def _append_url_variant(urls: list[str], raw_url: str) -> None:
    url = raw_url.strip().rstrip("/")
    if not url:
        return
    for variant in (url, _without_git_suffix(url)):
        if variant and variant not in urls:
            urls.append(variant)


def _without_git_suffix(url: str) -> str:
    return url[:-4] if url.endswith(".git") else url


def _canonical_repo_url(url: str) -> str:
    return _without_git_suffix(url.strip().rstrip("/"))


def _json_list(value: object, label: str) -> list[object]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RuntimeRepoRegistryError(f"{label} must be a list")
    return value
