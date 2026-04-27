from pydantic import BaseModel, Field


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
