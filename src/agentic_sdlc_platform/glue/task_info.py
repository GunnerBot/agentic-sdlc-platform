from dataclasses import dataclass

from agentic_sdlc_platform.glue.human_override import TaskInfoCommand


@dataclass(frozen=True)
class TaskInfoResult:
    task_id: str | None
    external_id: str
    command: str
    answer: str


class TaskInfoHandler:
    def __init__(self, repository) -> None:
        self._repository = repository

    async def handle(self, command: TaskInfoCommand) -> TaskInfoResult:
        task = await self._repository.find_task_by_external_id(command.external_id)
        if task is None:
            return TaskInfoResult(
                task_id=None,
                external_id=command.external_id,
                command=command.command,
                answer=f"Task {command.external_id} was not found.",
            )
        if command.command == "context":
            answer = await self.context_reply(task)
        elif command.command == "agents":
            answer = agents_reply(task)
        elif command.command == "nodes":
            answer = nodes_reply(task)
        else:
            answer = status_reply(task)
        return TaskInfoResult(
            task_id=task.id,
            external_id=task.external_id,
            command=command.command,
            answer=answer,
        )

    async def context_reply(self, task) -> str:
        repo_summary = "none"
        if task.repo:
            repo = await self._repository.get_repo_by_name(task.repo)
            if repo is None:
                repo_summary = f"{task.repo} (unregistered)"
            else:
                repo_summary = f"{repo.name} ({repo.provider}, {repo.default_branch})"

        events = [
            event
            for session in task.sessions
            for event in sorted(session.events, key=lambda item: (item.created_at, item.id))
            if not event.event_type.endswith("_command")
        ]
        recent_events = events[-3:]
        event_lines = [
            f"- {event.actor} {event.event_type}: {_single_line(event.message)}"
            for event in recent_events
        ]
        if not event_lines:
            event_lines = ["- none"]
        return "\n".join(
            [
                f"Task {task.external_id} context:",
                f"Repo: {repo_summary}",
                "Recent events:",
                *event_lines,
            ]
        )


def status_reply(task) -> str:
    active_sessions = sum(1 for session in task.sessions if session.status == "active")
    session_word = "session" if active_sessions == 1 else "sessions"
    return (
        f"Task {task.external_id} status: {task.status}. "
        f"Orchestrator: {_orchestrator_summary(task)}. "
        f"Repo: {task.repo or 'none'}. "
        f"Sessions: {active_sessions} active {session_word}. "
        f"{dag_progress_summary(task)}"
    )


def agents_reply(task) -> str:
    session_lines = []
    for session in task.sessions:
        session_lines.append(
            "- "
            f"{session.provider} session {session.id}: "
            f"status {session.status}, "
            f"repo {session.repo or 'none'}, "
            f"hermes {session.hermes_session_id or 'none'}, "
            f"events {len(session.events)}"
        )
    if not session_lines:
        session_lines = ["- none"]
    return "\n".join(
        [
            f"Task {task.external_id} agents:",
            f"Orchestrator: {_orchestrator_summary(task)}",
            *session_lines,
        ]
    )


def nodes_reply(task) -> str:
    dags = getattr(task, "dags", [])
    if not dags:
        return f"Task {task.external_id} nodes:\n- none"
    dag = dags[0]
    completed = {node.node_key for node in dag.nodes if node.status in {"completed", "skipped"}}
    ready_nodes = [
        node
        for node in dag.nodes
        if node.status not in {"completed", "skipped", "failed"}
        and all(dependency in completed for dependency in node.depends_on)
    ]
    next_node = ready_nodes[0].node_key if ready_nodes else "none"
    node_lines = [f"Next runnable: {next_node}"]
    for node in dag.nodes:
        depends_on = ",".join(node.depends_on) if node.depends_on else "none"
        orchestrator = node.orchestrator_task_id or "none"
        metadata = dict(getattr(node, "metadata_json", {}) or {})
        pr = _pr_summary(metadata)
        failure = _str_or_none(metadata.get("failure_error")) or "none"
        node_lines.append(
            "- "
            f"{node.node_key}: {node.status}; "
            f"repo {node.repo or 'none'}; "
            f"depends_on {depends_on}; "
            f"orchestrator {orchestrator}; "
            f"pr {pr}; "
            f"failure {failure}"
        )
    return "\n".join(
        [
            f"Task {task.external_id} nodes:",
            *node_lines,
        ]
    )


def dag_progress_summary(task) -> str:
    dags = getattr(task, "dags", [])
    if not dags:
        return "DAG: none."
    dag = dags[0]
    completed = {node.node_key for node in dag.nodes if node.status == "completed"}
    skipped = {node.node_key for node in dag.nodes if node.status == "skipped"}
    failed = {node.node_key for node in dag.nodes if node.status == "failed"}
    terminal = completed | skipped
    ready_nodes = [
        node
        for node in dag.nodes
        if node.status not in {"completed", "skipped", "failed"}
        and all(dependency in terminal for dependency in node.depends_on)
    ]
    next_node = ready_nodes[0].node_key if ready_nodes else "none"
    return (
        f"DAG: {dag.status}, {len(completed)}/{len(dag.nodes)} completed, "
        f"{len(skipped)} skipped, {len(failed)} failed, "
        f"{len(ready_nodes)} ready, next: {next_node}."
    )


def _orchestrator_summary(task) -> str:
    if not task.orchestrator_task_id:
        return "none"
    status = task.orchestrator_status or "unknown"
    return f"{task.orchestrator_task_id} ({status})"


def _pr_summary(metadata: dict[str, object]) -> str:
    url = _str_or_none(metadata.get("pr_url"))
    number = metadata.get("pr_number")
    state = _str_or_none(metadata.get("pr_state")) or "none"
    if isinstance(number, int):
        return f"#{number} ({state})"
    if url:
        return f"{url} ({state})"
    return state


def _single_line(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.split())


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None
