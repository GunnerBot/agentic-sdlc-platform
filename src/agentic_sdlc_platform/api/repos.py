from fastapi import APIRouter, HTTPException, Query, Request, status

from agentic_sdlc_platform.models.repos import RepoResponse, UpsertRepoRequest
from agentic_sdlc_platform.persistence.models import RepositoryRecord

router = APIRouter(tags=["repos"])


@router.post(
    "",
    response_model=RepoResponse,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_400_BAD_REQUEST: {"description": "Malformed request body"}},
)
async def upsert_repo(body: UpsertRepoRequest, request: Request) -> RepoResponse:
    repo = await request.app.state.repository.upsert_repo(
        name=body.name,
        provider=body.provider,
        clone_url=body.clone_url,
        default_branch=body.default_branch,
        metadata=body.metadata,
        status=body.status,
    )
    return _repo_response(repo)


@router.get("", response_model=list[RepoResponse])
async def list_repos(
    request: Request,
    provider: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
) -> list[RepoResponse]:
    repos = await request.app.state.repository.list_repos(
        provider=provider,
        status=status_filter,
    )
    return [_repo_response(repo) for repo in repos]


@router.get("/{repo_name}", response_model=RepoResponse)
async def get_repo(repo_name: str, request: Request) -> RepoResponse:
    repo = await request.app.state.repository.get_repo_by_name(repo_name)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )
    return _repo_response(repo)


def _repo_response(repo: RepositoryRecord) -> RepoResponse:
    return RepoResponse(
        id=repo.id,
        name=repo.name,
        provider=repo.provider,
        clone_url=repo.clone_url,
        default_branch=repo.default_branch,
        status=repo.status,
        metadata=repo.metadata_json,
    )
