from fastapi import HTTPException, status

from agentic_sdlc_platform.glue.channel_router import RepoQuery
from agentic_sdlc_platform.ports.graph_store import GraphQuery, GraphStoreError


async def answer_repo_query(repo_query: RepoQuery, repository, graph_store) -> dict[str, object]:
    repo = await repository.get_repo_by_name(repo_query.repo)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )
    try:
        result = await graph_store.query(
            GraphQuery(
                repo=repo.name,
                question=repo_query.question,
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
    return {
        "route": "graph_repo_query",
        "repo": repo.name,
        "answer": result.answer,
        "references": result.references,
    }
