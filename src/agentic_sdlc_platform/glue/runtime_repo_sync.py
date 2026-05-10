import asyncio
from collections.abc import Iterable

from agentic_sdlc_platform.persistence.models import RepositoryRecord
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.runtime_repo_registry import (
    RuntimeRepoRegistryError,
    RuntimeRepoRegistryPort,
    RuntimeRepository,
    RuntimeRepoSyncResult,
)

ACTIVE_RUNTIME_NODE_STATUSES = {
    "pending",
    "queued",
    "dispatched",
    "running",
    "needs_input",
    "needs_changes",
    "pr_open",
    "in_review",
}

_RUNTIME_REPO_SYNC_LOCK = asyncio.Lock()


async def sync_runtime_repositories_for_execution(
    *,
    repository: PersistenceRepository,
    runtime_repo_registry: RuntimeRepoRegistryPort | None,
    requested_repo: str | None,
) -> RuntimeRepoSyncResult | None:
    if runtime_repo_registry is None:
        return None

    async with _RUNTIME_REPO_SYNC_LOCK:
        repo_names = [
            *([requested_repo] if requested_repo else []),
            *await _active_runtime_repo_names(repository),
        ]
        repos = await _runtime_repositories_for_names(repository, repo_names, requested_repo)
        return await runtime_repo_registry.sync_repositories(repos)


async def _active_runtime_repo_names(repository: PersistenceRepository) -> list[str]:
    tasks = await repository.list_tasks()
    repo_names: list[str] = []
    for task in tasks:
        for dag in getattr(task, "dags", []):
            for node in getattr(dag, "nodes", []):
                if node.status in ACTIVE_RUNTIME_NODE_STATUSES and node.repo:
                    repo_names.append(node.repo)
    return repo_names


async def _runtime_repositories_for_names(
    repository: PersistenceRepository,
    repo_names: Iterable[str],
    requested_repo: str | None,
) -> list[RuntimeRepository]:
    runtime_repos: list[RuntimeRepository] = []
    seen: set[str] = set()
    for repo_name in repo_names:
        if repo_name in seen:
            continue
        seen.add(repo_name)
        repo = await repository.get_repo_by_name(repo_name)
        if repo is None or repo.status != "active":
            if repo_name == requested_repo:
                raise RuntimeRepoRegistryError(
                    f"runtime repository {requested_repo!r} is not registered as active"
                )
            continue
        runtime_repos.append(runtime_repository_from_record(repo))
    return runtime_repos


def runtime_repository_from_record(repo: RepositoryRecord) -> RuntimeRepository:
    return RuntimeRepository(
        name=repo.name,
        provider=repo.provider,
        clone_url=repo.clone_url,
        description=repo.name,
        metadata=dict(repo.metadata_json),
    )
