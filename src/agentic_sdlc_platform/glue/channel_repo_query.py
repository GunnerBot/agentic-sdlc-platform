from fastapi import HTTPException, status

from agentic_sdlc_platform.glue.channel_router import RepoQuery
from agentic_sdlc_platform.ports.graph_store import GraphQuery, GraphStoreError


async def answer_repo_query(repo_query: RepoQuery, repository, graph_store) -> dict[str, object]:
    return await answer_repo_question(
        repo=repo_query.repo,
        question=repo_query.question,
        repository=repository,
        graph_store=graph_store,
    )


async def answer_repo_question(
    *,
    repo: str,
    question: str,
    repository,
    graph_store,
) -> dict[str, object]:
    repo_record = await repository.get_repo_by_name(repo)
    if repo_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )
    try:
        result = await graph_store.query(
            GraphQuery(
                repo=repo_record.name,
                question=question,
                metadata={
                    **{key: str(value) for key, value in repo_record.metadata_json.items()},
                    "default_branch": repo_record.default_branch,
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
        "repo": repo_record.name,
        "answer": result.answer,
        "references": result.references,
    }
