from __future__ import annotations

from html import escape

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from agentic_sdlc_platform.glue.llm_observability import (
    LLM_COST_LEDGER_ARTIFACT_KIND,
    summarize_usage_records,
    usage_records_from_ledger_artifacts,
)

router = APIRouter(include_in_schema=False)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard_index(request: Request) -> HTMLResponse:
    tasks = await request.app.state.repository.list_tasks()
    rows = [
        [
            _link(f"/dashboard/tasks/{task.id}", task.external_id),
            _text(task.title),
            _text(task.repo or ""),
            _text(task.status),
            _text(len(getattr(task, "dags", []) or [])),
            _text(len(getattr(task, "artifacts", []) or [])),
        ]
        for task in tasks
    ]
    body = (
        "<h1>agentic-sdlc-platform</h1>"
        "<section><h2>Tasks</h2>"
        + _table(
            ["Task", "Title", "Repo", "Status", "DAGs", "Artifacts"],
            rows,
            empty="No tasks have been created yet.",
        )
        + "</section>"
    )
    return HTMLResponse(_layout("Dashboard", body))


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def dashboard_task_detail(task_id: str, request: Request) -> HTMLResponse:
    task = await request.app.state.repository.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    body = (
        f"<h1>{_text(task.external_id)}</h1>"
        f"<p>{_text(task.title)}</p>"
        f"<p>Status: <strong>{_text(task.status)}</strong>"
        f" | Repo: <strong>{_text(task.repo or '')}</strong></p>"
        f"<section><h2>DAGs</h2>{_dag_table(task)}</section>"
        f"<section><h2>PRs</h2>{_pr_table(task)}</section>"
        f"<section><h2>Cost</h2>{_cost_table(task)}</section>"
        f"<section><h2>Artifacts</h2>{_artifact_table(task)}</section>"
        f"<section><h2>Conversations</h2>{_conversation_table(task)}</section>"
    )
    return HTMLResponse(_layout(f"Task {task.external_id}", body))


def _dag_table(task) -> str:
    rows: list[list[str]] = []
    for dag in getattr(task, "dags", []) or []:
        for node in getattr(dag, "nodes", []) or []:
            rows.append(
                [
                    _text(dag.id),
                    _text(node.node_key),
                    _text(node.title),
                    _text(node.repo or ""),
                    _text(node.status),
                    _text(", ".join(getattr(node, "depends_on", []) or []) or "none"),
                ]
            )
    return _table(
        ["DAG", "Node", "Title", "Repo", "Status", "Depends On"],
        rows,
        empty="No DAG nodes.",
    )


def _pr_table(task) -> str:
    rows: list[list[str]] = []
    for dag in getattr(task, "dags", []) or []:
        for node in getattr(dag, "nodes", []) or []:
            metadata = dict(getattr(node, "metadata_json", {}) or {})
            pr_url = metadata.get("pr_url")
            pr_number = metadata.get("pr_number")
            if not pr_url and not pr_number:
                continue
            rows.append(
                [
                    _text(dag.id),
                    _text(node.node_key),
                    _text(pr_number or ""),
                    _link(str(pr_url), str(pr_url)) if isinstance(pr_url, str) else "",
                    _text(metadata.get("pr_state") or ""),
                    _text(metadata.get("expected_pr_reference") or ""),
                ]
            )
    return _table(
        ["DAG", "Node", "PR", "URL", "State", "Reference"],
        rows,
        empty="No PRs have been recorded yet.",
    )


def _cost_table(task) -> str:
    ledger_artifacts = [
        artifact
        for artifact in getattr(task, "artifacts", []) or []
        if artifact.kind == LLM_COST_LEDGER_ARTIFACT_KIND
    ]
    records = usage_records_from_ledger_artifacts(ledger_artifacts)
    summary = summarize_usage_records(records)
    rows = [
        [
            _text(summary["total_input_tokens"]),
            _text(summary["total_output_tokens"]),
            _text(summary["total_tokens"]),
            _text(f"${summary['total_estimated_cost_usd']:.6f}"),
        ]
    ]
    return _table(
        ["Input Tokens", "Output Tokens", "Total Tokens", "Estimated Cost"],
        rows,
    )


def _artifact_table(task) -> str:
    rows = [
        [
            _text(artifact.kind),
            _text(artifact.name),
            _text(artifact.dag_id or ""),
            _text(artifact.node_key or ""),
            _text(dict(artifact.metadata_json).get("status") or ""),
        ]
        for artifact in getattr(task, "artifacts", []) or []
    ]
    return _table(
        ["Kind", "Name", "DAG", "Node", "Status"],
        rows,
        empty="No artifacts.",
    )


def _conversation_table(task) -> str:
    rows: list[list[str]] = []
    for session in getattr(task, "sessions", []) or []:
        events = getattr(session, "events", []) or []
        if not events:
            rows.append(
                [
                    _text(session.provider),
                    _text(session.external_thread_id),
                    _text(session.status),
                    "",
                    "",
                    "",
                ]
            )
        for event in events:
            rows.append(
                [
                    _text(session.provider),
                    _text(session.external_thread_id),
                    _text(session.status),
                    _text(event.actor),
                    _text(event.event_type),
                    _text(event.message or ""),
                ]
            )
    return _table(
        ["Provider", "Thread", "Session Status", "Actor", "Type", "Message"],
        rows,
        empty="No conversations.",
    )


def _table(headers: list[str], rows: list[list[str]], *, empty: str = "") -> str:
    if not rows:
        return f"<p>{_text(empty)}</p>"
    header_html = "".join(f"<th>{_text(header)}</th>" for header in headers)
    rows_html = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{header_html}</tr></thead><tbody>{rows_html}</tbody></table>"


def _link(href: str, label: str) -> str:
    return f'<a href="{escape(href, quote=True)}">{_text(label)}</a>'


def _text(value: object) -> str:
    return escape(str(value), quote=True)


def _layout(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head>"
        f"<title>{_text(title)}</title>"
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "margin:32px;color:#17202a;background:#f8fafc}"
        "a{color:#0f5bd8;text-decoration:none}"
        "section{margin:28px 0}"
        "table{width:100%;border-collapse:collapse;background:white}"
        "th,td{border:1px solid #d8dee9;padding:8px 10px;text-align:left;"
        "vertical-align:top}"
        "th{background:#eef2f7}"
        "</style></head><body>"
        f"{body}"
        "</body></html>"
    )
