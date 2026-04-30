from dataclasses import dataclass, field
from typing import Protocol


class SourceControlError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceRepository:
    name: str
    full_name: str
    clone_url: str | None
    html_url: str | None
    default_branch: str
    private: bool
    permissions: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceInstallation:
    provider: str
    installation_id: str
    account: str | None
    repositories: list[SourceRepository]


class SourceControlPort(Protocol):
    provider: str

    async def list_installation_repositories(
        self,
        installation_id: str | None = None,
    ) -> SourceInstallation:
        raise NotImplementedError
