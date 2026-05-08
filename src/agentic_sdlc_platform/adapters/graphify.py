import asyncio
import base64
import os
import shlex
import shutil
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

import httpx

from agentic_sdlc_platform.adapters.github_app import GitHubAppSourceControl
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.graph_store import (
    GraphIndexRequest,
    GraphIndexResult,
    GraphQuery,
    GraphQueryResult,
    GraphStoreError,
)

CommandRunner = Callable[[list[str], float], Awaitable[str]]
GitCommandRunner = Callable[[list[str], float, dict[str, str] | None], Awaitable[str]]
InstallationTokenProvider = Callable[[str], Awaitable[str]]


class GraphifyGraphStore:
    """Graphify graph store adapter.

    Graphify's official integration is CLI/MCP-first. This adapter uses the real
    `graphify` command by default, while keeping an HTTP mode for compatible
    self-hosted wrappers.
    """

    provider = "graphify"

    def __init__(
        self,
        settings: Settings,
        runner: CommandRunner | None = None,
        git_runner: GitCommandRunner | None = None,
        installation_token_provider: InstallationTokenProvider | None = None,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._runner = runner or _run_command
        self._git_runner = git_runner or _run_git_command
        self._installation_token_provider = (
            installation_token_provider or self._github_installation_token
        )
        self._transport = transport

    async def index(self, request: GraphIndexRequest) -> GraphIndexResult:
        self._ensure_enabled()
        if self._mode == "http":
            return await self._http_index(request)

        source_path = self._repo_path(request.metadata)
        if source_path is None:
            source_path = await self._cached_repo_path(request)
        repo_path = self._index_path(request.repo, source_path)

        command = [
            *self._command_parts(),
            "update",
            str(repo_path),
        ]
        await self._runner(command, self._settings.graphify_timeout_seconds)
        graph_path = self._graph_path_for_indexed_repo(request.repo, repo_path)
        if not graph_path.exists():
            raise GraphStoreError(f"graphify did not produce graph at {graph_path}")
        return GraphIndexResult(
            provider=self.provider,
            external_index_id=str(graph_path),
            status="indexed",
        )

    async def query(self, request: GraphQuery) -> GraphQueryResult:
        self._ensure_enabled()
        if self._mode == "http":
            return await self._http_query(request)

        graph_path = self._graph_path(request.repo, request.metadata)
        if graph_path is None:
            raise GraphStoreError(
                "graphify CLI query requires graph_path or repo local_path metadata"
            )
        if not graph_path.exists():
            raise GraphStoreError(f"graphify graph does not exist at {graph_path}")
        command = [
            *self._command_parts(),
            "query",
            request.question,
            "--graph",
            str(graph_path),
        ]
        output = await self._runner(command, self._settings.graphify_timeout_seconds)
        return GraphQueryResult(
            provider=self.provider,
            answer=output.strip(),
            references=_references_from_output(output),
        )

    async def _http_index(self, request: GraphIndexRequest) -> GraphIndexResult:
        response = await self._request_with_retries(
            method="POST",
            path="/api/index",
            payload={
                "repo": request.repo,
                "clone_url": request.clone_url,
                "default_branch": request.default_branch,
                "metadata": request.metadata,
            },
            failure_message="graphify index failed",
        )
        payload = _json_dict(response.json(), "graphify index")
        return GraphIndexResult(
            provider=_str(payload.get("provider")) or self.provider,
            external_index_id=_required_str(payload, "external_index_id", "graphify index"),
            status=_str(payload.get("status")) or "indexed",
        )

    async def _http_query(self, request: GraphQuery) -> GraphQueryResult:
        response = await self._request_with_retries(
            method="POST",
            path="/api/query",
            payload={
                "repo": request.repo,
                "question": request.question,
                "metadata": request.metadata,
            },
            failure_message="graphify query failed",
        )
        payload = _json_dict(response.json(), "graphify query")
        references = payload.get("references")
        return GraphQueryResult(
            provider=_str(payload.get("provider")) or self.provider,
            answer=_required_str(payload, "answer", "graphify query"),
            references=[item for item in references if isinstance(item, str)]
            if isinstance(references, list)
            else [],
        )

    async def _request_with_retries(
        self,
        *,
        method: str,
        path: str,
        payload: dict[str, object | None],
        failure_message: str,
    ) -> httpx.Response:
        if not self._settings.graphify_base_url:
            raise GraphStoreError("graphify base URL is not configured")

        attempts = self._settings.graphify_max_retries + 1
        try:
            async with httpx.AsyncClient(
                base_url=self._settings.graphify_base_url,
                timeout=self._settings.graphify_timeout_seconds,
                transport=self._transport,
            ) as client:
                for attempt in range(attempts):
                    response = await client.request(
                        method,
                        path,
                        json=payload,
                        headers=self._headers(),
                    )
                    if response.status_code < 500:
                        response.raise_for_status()
                        return response
                    if attempt == attempts - 1:
                        response.raise_for_status()
                    await asyncio.sleep(0.25 * (2**attempt))
        except httpx.HTTPError as exc:
            raise GraphStoreError(failure_message) from exc
        raise GraphStoreError(failure_message)

    def _ensure_enabled(self) -> None:
        if not self._settings.vendor_http_enabled:
            raise GraphStoreError("graph store access is disabled")
        if self._mode not in {"cli", "http"}:
            raise GraphStoreError(f"unsupported graphify mode {self._mode!r}")

    @property
    def _mode(self) -> str:
        return self._settings.graphify_mode.strip().lower()

    def _headers(self) -> dict[str, str]:
        headers = {"X-Client-Platform": "agentic-sdlc-platform"}
        if self._settings.graphify_api_key:
            headers["Authorization"] = f"Bearer {self._settings.graphify_api_key}"
        return headers

    def _command_parts(self) -> list[str]:
        command = shlex.split(self._settings.graphify_command)
        if not command:
            raise GraphStoreError("graphify command is not configured")
        return command

    def _repo_path(self, metadata: dict[str, str]) -> Path | None:
        for key in ("local_path", "repo_path", "workspace_path"):
            value = metadata.get(key)
            if value:
                repo_path = Path(value).expanduser()
                if repo_path.exists():
                    return repo_path
        return None

    async def _cached_repo_path(self, request: GraphIndexRequest) -> Path:
        if not self._settings.repo_cache_root:
            raise GraphStoreError(
                "graphify CLI indexing requires repo metadata local_path, repo_path, "
                "or repo cache root"
            )
        if not request.clone_url:
            raise GraphStoreError("repo clone URL is required for repo cache indexing")

        target_path = self._repo_cache_path(request.repo)
        token = await self._installation_token(request.metadata)
        env = _git_auth_env(token) if token else None
        if (target_path / ".git").exists():
            await self._fetch_cached_repo(
                repo_path=target_path,
                default_branch=request.default_branch,
                env=env,
            )
            return target_path

        if target_path.exists():
            shutil.rmtree(target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        await self._git_runner(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                request.default_branch,
                request.clone_url,
                str(target_path),
            ],
            self._settings.repo_cache_clone_timeout_seconds,
            env,
        )
        return target_path

    async def _fetch_cached_repo(
        self,
        *,
        repo_path: Path,
        default_branch: str,
        env: dict[str, str] | None,
    ) -> None:
        timeout = self._settings.repo_cache_clone_timeout_seconds
        await self._git_runner(
            ["git", "-C", str(repo_path), "fetch", "--depth", "1", "origin", default_branch],
            timeout,
            env,
        )
        await self._git_runner(
            ["git", "-C", str(repo_path), "reset", "--hard", "FETCH_HEAD"],
            timeout,
            env,
        )
        await self._git_runner(
            ["git", "-C", str(repo_path), "clean", "-fdx"],
            timeout,
            env,
        )

    async def _installation_token(self, metadata: dict[str, str]) -> str | None:
        installation_id = metadata.get("github_app_installation_id")
        if not installation_id:
            return None
        return await self._installation_token_provider(installation_id)

    async def _github_installation_token(self, installation_id: str) -> str:
        try:
            return await GitHubAppSourceControl(
                self._settings,
                transport=self._transport,
            ).installation_token(installation_id)
        except Exception as exc:
            raise GraphStoreError("github app installation token request failed") from exc

    def _graph_path(self, repo: str, metadata: dict[str, str]) -> Path | None:
        graph_path = metadata.get("graph_path") or metadata.get("graphify_graph_path")
        if graph_path:
            return Path(graph_path).expanduser()

        repo_path = self._repo_path(metadata)
        if repo_path is None:
            if self._settings.graphify_output_root:
                return self._graph_path_for_indexed_repo(
                    repo,
                    self._output_repo_path(repo),
                )
            return None
        return self._graph_path_for_indexed_repo(repo, repo_path)

    def _index_path(self, repo: str, source_path: Path) -> Path:
        if not self._settings.graphify_output_root:
            return source_path

        target_path = self._output_repo_path(repo)
        if target_path.exists():
            shutil.rmtree(target_path)
        shutil.copytree(
            source_path,
            target_path,
            ignore_dangling_symlinks=True,
            ignore=shutil.ignore_patterns(
                ".git",
                ".venv",
                "node_modules",
                "dist",
                "build",
                "graphify-out",
            ),
        )
        return target_path

    def _graph_path_for_indexed_repo(self, repo: str, repo_path: Path) -> Path:
        if self._settings.graphify_output_root:
            return self._output_repo_path(repo) / "graphify-out" / "graph.json"
        return repo_path / "graphify-out" / "graph.json"

    def _output_repo_path(self, repo: str) -> Path:
        if not self._settings.graphify_output_root:
            raise GraphStoreError("graphify output root is not configured")
        return Path(self._settings.graphify_output_root).expanduser() / _safe_repo_name(repo)

    def _repo_cache_path(self, repo: str) -> Path:
        if not self._settings.repo_cache_root:
            raise GraphStoreError("repo cache root is not configured")
        return Path(self._settings.repo_cache_root).expanduser() / _safe_repo_path(repo)


async def _run_command(
    command: list[str],
    command_timeout: float,
    env: Mapping[str, str] | None = None,
) -> str:
    try:
        process_env = None
        if env is not None:
            process_env = {**os.environ, **env}
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=process_env,
        )
    except OSError as exc:
        raise GraphStoreError(f"graphify command failed to start: {command[0]}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=command_timeout,
        )
    except TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise GraphStoreError("graphify command timed out") from exc

    if process.returncode != 0:
        error_text = stderr.decode("utf-8", errors="replace").strip()
        error_text = _redact_env_values(error_text, env)
        raise GraphStoreError(error_text or "graphify command failed")
    return stdout.decode("utf-8", errors="replace")


async def _run_git_command(
    command: list[str],
    command_timeout: float,
    env: dict[str, str] | None,
) -> str:
    return await _run_command(command, command_timeout, env=env)


def _git_auth_env(token: str) -> dict[str, str]:
    basic_token = base64.b64encode(f"x-access-token:{token}".encode()).decode(
        "ascii"
    )
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic_token}",
    }


def _redact_env_values(error_text: str, env: Mapping[str, str] | None) -> str:
    if env is None:
        return error_text
    redacted = error_text
    for key, value in env.items():
        if not value:
            continue
        if "TOKEN" in key or "KEY" in key or "SECRET" in key or "VALUE" in key:
            redacted = redacted.replace(value, "<redacted>")
    return redacted


def _safe_repo_name(repo: str) -> str:
    return repo.replace("/", "__")


def _safe_repo_path(repo: str) -> Path:
    parts = [part for part in repo.split("/") if part]
    if len(parts) < 2:
        return Path(_safe_repo_name(repo))
    return Path(*[_safe_path_part(part) for part in parts])


def _safe_path_part(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value)


def _references_from_output(output: str) -> list[str]:
    references: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("- ", "* ")):
            stripped = stripped[2:].strip()
        if "/" in stripped and any(stripped.endswith(suffix) for suffix in _SOURCE_SUFFIXES):
            references.append(stripped)
        elif ":" in stripped and any(suffix in stripped for suffix in _SOURCE_SUFFIXES):
            references.append(stripped)
    return list(dict.fromkeys(references))


_SOURCE_SUFFIXES = (
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".rb",
    ".php",
    ".md",
    ".yml",
    ".yaml",
    ".json",
)


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _json_dict(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise GraphStoreError(f"{label} response was not an object")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _required_str(payload: dict[str, object], key: str, label: str) -> str:
    value = _str(payload.get(key))
    if value is None:
        raise GraphStoreError(f"{label} response did not include {key}")
    return value
