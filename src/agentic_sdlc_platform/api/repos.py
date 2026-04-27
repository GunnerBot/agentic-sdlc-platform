from fastapi import APIRouter, HTTPException, Query, Request, status

from agentic_sdlc_platform.models.repos import (
    GitHubAppImportResponse,
    GitHubAppInstallationResponse,
    GitHubAppRepositoryResponse,
    RepoIndexAllResponse,
    RepoIndexJobResponse,
    RepoQuestionRequest,
    RepoQuestionResponse,
    RepoResponse,
    UpsertRepoRequest,
)
from agentic_sdlc_platform.persistence.models import RepoIndexJob, RepositoryRecord
from agentic_sdlc_platform.ports.graph_store import GraphIndexRequest, GraphQuery, GraphStoreError
from agentic_sdlc_platform.ports.source_control import SourceControlError, SourceInstallation

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


@router.post(
    "/index-all",
    response_model=RepoIndexAllResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def index_all_repos(request: Request) -> RepoIndexAllResponse:
    repos = await request.app.state.repository.list_repos(status="active")
    jobs = [await _index_repo_record(repo, request) for repo in repos]
    return RepoIndexAllResponse(
        total=len(jobs),
        indexed=len([job for job in jobs if job.status == "indexed"]),
        failed=len([job for job in jobs if job.status == "failed"]),
        jobs=[_index_job_response(job) for job in jobs],
    )


@router.get(
    "/github-app/installation",
    response_model=GitHubAppInstallationResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "GitHub App unavailable"}},
)
async def get_github_app_installation(request: Request) -> GitHubAppInstallationResponse:
    installation = await _github_app_installation(request)
    return _github_app_installation_response(installation)


@router.post(
    "/github-app/import",
    response_model=GitHubAppImportResponse,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "GitHub App unavailable"}},
)
async def import_github_app_repositories(request: Request) -> GitHubAppImportResponse:
    installation = await _github_app_installation(request)
    imported = []
    for repo in installation.repositories:
        imported.append(
            await request.app.state.repository.upsert_repo(
                name=repo.full_name,
                provider="github",
                clone_url=repo.clone_url,
                default_branch=repo.default_branch,
                status="active",
                metadata={
                    "github_app_installation_id": installation.installation_id,
                    "github_app_account": installation.account,
                    "github_html_url": repo.html_url,
                    "github_private": repo.private,
                    "github_permissions": repo.permissions,
                    "write_enabled": False,
                    "write_enablement_todo": (
                        "Enable GitHub App write scopes before automatic branch push "
                        "and PR creation."
                    ),
                },
            )
        )
    return GitHubAppImportResponse(
        imported=len(imported),
        repositories=[_repo_response(repo) for repo in imported],
    )


@router.get("/index-all", include_in_schema=False)
async def reject_get_index_all() -> None:
    raise HTTPException(
        status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
        headers={"Allow": "POST"},
    )


@router.get("/{repo_name}", response_model=RepoResponse)
async def get_repo(repo_name: str, request: Request) -> RepoResponse:
    repo = await request.app.state.repository.get_repo_by_name(repo_name)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )
    return _repo_response(repo)


@router.post(
    "/{repo_name}/ask",
    response_model=RepoQuestionResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Repository not found"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Graph store unavailable"},
    },
)
async def ask_repo(
    repo_name: str,
    body: RepoQuestionRequest,
    request: Request,
) -> RepoQuestionResponse:
    repo = await request.app.state.repository.get_repo_by_name(repo_name)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )
    try:
        result = await request.app.state.graph_store.query(
            GraphQuery(
                repo=repo.name,
                question=body.question,
                metadata={
                    **{key: str(value) for key, value in repo.metadata_json.items()},
                    "default_branch": repo.default_branch,
                },
            )
        )
    except GraphStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return RepoQuestionResponse(
        provider=result.provider,
        answer=result.answer,
        references=result.references,
    )


@router.post(
    "/{repo_name}/index",
    response_model=RepoIndexJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={status.HTTP_404_NOT_FOUND: {"description": "Repository not found"}},
)
async def index_repo(repo_name: str, request: Request) -> RepoIndexJobResponse:
    repo = await request.app.state.repository.get_repo_by_name(repo_name)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    job = await _index_repo_record(repo, request)
    return _index_job_response(job)


@router.get("/{repo_name}/index-jobs", response_model=list[RepoIndexJobResponse])
async def list_repo_index_jobs(repo_name: str, request: Request) -> list[RepoIndexJobResponse]:
    jobs = await request.app.state.repository.list_repo_index_jobs(repo_name=repo_name)
    return [_index_job_response(job) for job in jobs]


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


async def _github_app_installation(request: Request) -> SourceInstallation:
    source_control = request.app.state.source_control
    if source_control is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub App read-only integration is not configured",
        )
    try:
        return await source_control.list_installation_repositories()
    except SourceControlError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


def _github_app_installation_response(
    installation: SourceInstallation,
) -> GitHubAppInstallationResponse:
    return GitHubAppInstallationResponse(
        provider=installation.provider,
        installation_id=installation.installation_id,
        account=installation.account,
        repositories=[
            GitHubAppRepositoryResponse(
                name=repo.name,
                full_name=repo.full_name,
                clone_url=repo.clone_url,
                html_url=repo.html_url,
                default_branch=repo.default_branch,
                private=repo.private,
                permissions=repo.permissions,
            )
            for repo in installation.repositories
        ],
    )


async def _index_repo_record(repo: RepositoryRecord, request: Request) -> RepoIndexJob:
    job = await request.app.state.repository.create_repo_index_job(
        repo_name=repo.name,
        provider="graphify",
        metadata={"default_branch": repo.default_branch},
    )
    try:
        result = await request.app.state.graph_store.index(
            GraphIndexRequest(
                repo=repo.name,
                clone_url=repo.clone_url,
                default_branch=repo.default_branch,
                metadata={key: str(value) for key, value in repo.metadata_json.items()},
            )
        )
    except GraphStoreError as exc:
        return await request.app.state.repository.mark_repo_index_job_failed(
            job_id=job.id,
            error=str(exc),
        )

    return await request.app.state.repository.mark_repo_index_job_completed(
        job_id=job.id,
        external_index_id=result.external_index_id,
        status=result.status,
    )


def _index_job_response(job: RepoIndexJob) -> RepoIndexJobResponse:
    return RepoIndexJobResponse(
        id=job.id,
        repo_name=job.repo_name,
        provider=job.provider,
        external_index_id=job.external_index_id,
        status=job.status,
        error=job.error,
        metadata=job.metadata_json,
    )
