from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


class RuntimeRepoRegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeRepository:
    name: str
    provider: str
    clone_url: str | None = None
    description: str | None = None
    metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class RuntimeRepoSyncResult:
    provider: str
    workspace_id: str
    repo_count: int
    urls: tuple[str, ...]


class RuntimeRepoRegistryPort(Protocol):
    async def sync_repositories(
        self,
        repositories: Sequence[RuntimeRepository],
    ) -> RuntimeRepoSyncResult:
        raise NotImplementedError
