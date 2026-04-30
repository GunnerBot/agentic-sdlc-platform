from fastapi import APIRouter, HTTPException, Query, Request, status

from agentic_sdlc_platform.models.repos import (
    GitHubAppImportRequest,
    GitHubAppImportResponse,
    GitHubAppInstallationRecordResponse,
    GitHubAppInstallationResponse,
    GitHubAppInstallUrlResponse,
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
    "/github-app/install-url",
    response_model=GitHubAppInstallUrlResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "GitHub App unavailable"}},
)
async def get_github_app_install_url(
    request: Request,
    workspace_id: str = Query(default="default", min_length=1),
) -> GitHubAppInstallUrlResponse:
    app_slug = request.app.state.settings.github_app_slug
    if not app_slug:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub App slug is not configured",
        )
    return GitHubAppInstallUrlResponse(
        workspace_id=workspace_id,
        app_slug=app_slug,
        install_url=f"https://github.com/apps/{app_slug}/installations/new",
        instructions=(
            "Install the GitHub App, choose the account or organization, and select "
            "the repositories this workspace may read and write."
        ),
    )


@router.get(
    "/github-app/installation",
    response_model=GitHubAppInstallationResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "GitHub App unavailable"}},
)
async def get_github_app_installation(
    request: Request,
    installation_id: str | None = Query(default=None, min_length=1),
) -> GitHubAppInstallationResponse:
    installation = await _github_app_installation(
        request,
        installation_id=installation_id,
    )
    return _github_app_installation_response(installation)


@router.post(
    "/github-app/import",
    response_model=GitHubAppImportResponse,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "GitHub App unavailable"}},
)
async def import_github_app_repositories(request: Request) -> GitHubAppImportResponse:
    return await _import_github_app_repositories(
        request=request,
        body=GitHubAppImportRequest(),
    )


@router.post(
    "/github-app/sync",
    response_model=GitHubAppImportResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Malformed request body"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "GitHub App unavailable"},
    },
)
async def sync_github_app_repositories(
    body: GitHubAppImportRequest,
    request: Request,
) -> GitHubAppImportResponse:
    return await _import_github_app_repositories(request=request, body=body)


async def _import_github_app_repositories(
    *,
    request: Request,
    body: GitHubAppImportRequest,
) -> GitHubAppImportResponse:
    installation = await _github_app_installation(
        request,
        installation_id=body.installation_id,
    )
    installation_record = await request.app.state.repository.upsert_github_installation(
        workspace_id=body.workspace_id,
        installation_id=installation.installation_id,
        account=installation.account,
        repository_selection="selected",
        permissions=_installation_permissions(installation),
        status="active",
        metadata={
            "repo_count": len(installation.repositories),
            "single_app_read_write": True,
        },
    )
    imported = []
    for repo in installation.repositories:
        write_enabled = _repo_write_enabled(
            repo.permissions,
            request.app.state.settings.github_app_write_enabled_default,
        )
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
                    "workspace_id": body.workspace_id,
                    "read_enabled": True,
                    "write_enabled": write_enabled,
                    "allowed_branch_prefix": "agent/dag/",
                    "write_policy": _repo_write_policy(write_enabled),
                },
            )
        )
    return GitHubAppImportResponse(
        imported=len(imported),
        installation=_github_app_installation_record_response(installation_record),
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


async def _github_app_installation(
    request: Request,
    installation_id: str | None = None,
) -> SourceInstallation:
    source_control = request.app.state.source_control
    if source_control is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub App read-only integration is not configured",
        )
    try:
        return await source_control.list_installation_repositories(
            installation_id=installation_id,
        )
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


def _github_app_installation_record_response(
    installation,
) -> GitHubAppInstallationRecordResponse:
    return GitHubAppInstallationRecordResponse(
        id=installation.id,
        workspace_id=installation.workspace_id,
        provider=installation.provider,
        installation_id=installation.installation_id,
        account=installation.account,
        repository_selection=installation.repository_selection,
        status=installation.status,
        permissions=installation.permissions_json,
        metadata=installation.metadata_json,
    )


def _installation_permissions(installation: SourceInstallation) -> dict[str, object]:
    permission_names: dict[str, object] = {}
    for repo in installation.repositories:
        for key, value in repo.permissions.items():
            if value:
                permission_names[key] = True
            elif key not in permission_names:
                permission_names[key] = False
    return permission_names


def _repo_write_enabled(
    permissions: dict[str, bool],
    default_write_enabled: bool,
) -> bool:
    if permissions.get("push") is False:
        return False
    if permissions.get("contents_write") is True:
        return True
    if permissions.get("pull_requests_write") is True:
        return True
    if permissions.get("contents") is False or permissions.get("pull_requests") is False:
        return False
    return default_write_enabled


def _repo_write_policy(write_enabled: bool) -> dict[str, object]:
    return {
        "enabled": write_enabled,
        "branch_prefix": "agent/dag/",
        "direct_default_branch_push": False,
        "requires_plan_approval": True,
        "auto_merge_enabled": False,
        "requires_pr_body_reference": "dag/<dag_id>/<node_key>",
    }


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
