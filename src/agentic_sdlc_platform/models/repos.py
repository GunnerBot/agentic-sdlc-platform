from typing import Annotated

from pydantic import BaseModel, Field

RepoName = Annotated[str, Field(min_length=1, pattern=r".*\S.*")]


class UpsertRepoRequest(BaseModel):
    name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    clone_url: str | None = None
    default_branch: str = Field(default="main", min_length=1)
    status: str = Field(default="active", min_length=1)
    metadata: dict[str, object] = Field(default_factory=dict)


class RepoResponse(BaseModel):
    id: str
    name: str
    provider: str
    clone_url: str | None = None
    default_branch: str
    status: str
    metadata: dict[str, object]


class RepoIndexJobResponse(BaseModel):
    id: str
    repo_name: str
    provider: str
    external_index_id: str | None = None
    status: str
    error: str | None = None
    metadata: dict[str, object]


class RepoIndexSelectedRequest(BaseModel):
    repos: list[RepoName] = Field(min_length=1)


class RepoIndexAllResponse(BaseModel):
    total: int
    indexed: int
    failed: int
    jobs: list[RepoIndexJobResponse]


class RepoQuestionRequest(BaseModel):
    question: str = Field(min_length=1)


class RepoQuestionResponse(BaseModel):
    provider: str
    answer: str
    references: list[str]


class GitHubAppRepositoryResponse(BaseModel):
    name: str
    full_name: str
    clone_url: str | None = None
    html_url: str | None = None
    default_branch: str
    private: bool
    permissions: dict[str, bool]


class GitHubAppInstallationResponse(BaseModel):
    provider: str
    installation_id: str
    account: str | None = None
    repositories: list[GitHubAppRepositoryResponse]


class GitHubAppInstallUrlResponse(BaseModel):
    workspace_id: str
    install_url: str
    app_slug: str
    instructions: str


class GitHubAppImportRequest(BaseModel):
    workspace_id: str = Field(default="default", min_length=1)
    installation_id: str | None = Field(default=None, min_length=1)


class GitHubAppInstallationRecordResponse(BaseModel):
    id: str
    workspace_id: str
    provider: str
    installation_id: str
    account: str | None = None
    repository_selection: str
    status: str
    permissions: dict[str, object]
    metadata: dict[str, object]


class GitHubAppImportResponse(BaseModel):
    imported: int
    installation: GitHubAppInstallationRecordResponse | None = None
    repositories: list[RepoResponse]
