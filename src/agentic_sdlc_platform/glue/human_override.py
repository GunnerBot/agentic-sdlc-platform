import re
from dataclasses import dataclass

from fastapi import HTTPException, status

from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.task_orchestrator import TaskOrchestratorPort, TaskUpdateRequest

STATUS_BY_COMMAND = {
    "pause": "paused",
    "resume": "queued",
    "takeover": "human_takeover",
    "context": "context_requested",
    "reject": "rejected",
}


@dataclass(frozen=True)
class HumanOverrideCommand:
    command: str
    external_id: str
    reason: str | None = None


@dataclass(frozen=True)
class HumanOverrideResult:
    task_id: str
    command: str
    status: str


@dataclass(frozen=True)
class TaskInfoCommand:
    command: str
    external_id: str


@dataclass(frozen=True)
class NodeOverrideCommand:
    command: str
    external_id: str
    node_key: str
    reason: str | None = None


@dataclass(frozen=True)
class NodeOverrideResult:
    task_id: str
    dag_id: str
    node_key: str
    command: str
    status: str


@dataclass(frozen=True)
class PlanApprovalCommand:
    external_id: str


class HumanOverrideHandler:
    def __init__(
        self,
        repository: PersistenceRepository,
        task_orchestrator: TaskOrchestratorPort | None = None,
    ) -> None:
        self._repository = repository
        self._task_orchestrator = task_orchestrator

    async def handle(
        self,
        command: HumanOverrideCommand,
        actor: str,
        channel: str,
    ) -> HumanOverrideResult:
        task = await self._repository.find_task_by_external_id(command.external_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task not found for override command",
            )

        next_status = STATUS_BY_COMMAND[command.command]
        task = await self._repository.update_task_status(task_id=task.id, status=next_status)
        if task.orchestrator_task_id and self._task_orchestrator is not None:
            external_task = await self._task_orchestrator.update_task(
                TaskUpdateRequest(
                    external_task_id=task.orchestrator_task_id,
                    status=next_status,
                    metadata={
                        "command": command.command,
                        "actor": actor,
                        "channel": channel,
                        "reason": command.reason,
                    },
                )
            )
            task = await self._repository.mark_task_orchestrated(
                task_id=task.id,
                orchestrator_task_id=external_task.external_task_id,
                orchestrator_status=external_task.status,
            )

        await self._repository.record_audit_event(
            action=f"human_override.{command.command}",
            actor=actor,
            target_type="task",
            target_id=task.id,
            metadata={
                "external_id": command.external_id,
                "channel": channel,
                "reason": command.reason,
                "status": next_status,
            },
        )
        return HumanOverrideResult(
            task_id=task.id,
            command=command.command,
            status=next_status,
        )


class NodeOverrideHandler:
    def __init__(self, repository: PersistenceRepository) -> None:
        self._repository = repository

    async def handle(
        self,
        command: NodeOverrideCommand,
        actor: str,
        channel: str,
    ) -> NodeOverrideResult:
        task = await self._repository.find_task_by_external_id(command.external_id)
        if task is None or not getattr(task, "dags", []):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task DAG not found for node override command",
            )
        dag = task.dags[0]
        node = next((node for node in dag.nodes if node.node_key == command.node_key), None)
        if node is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="DAG node not found for override command",
            )

        metadata = {
            "override_actor": actor,
            "override_channel": channel,
            "override_reason": command.reason,
        }
        if command.command == "pause-node":
            node = await self._repository.update_dag_node_status(
                dag_id=dag.id,
                node_key=command.node_key,
                status="paused",
                orchestrator_status="paused",
                metadata=metadata,
            )
            await self._cancel_active_executions(dag.id, command.node_key, command.reason)
        elif command.command == "retry-node":
            node = await self._repository.retry_dag_node(
                dag_id=dag.id,
                node_key=command.node_key,
            )
        else:
            node = await self._repository.mark_dag_node_skipped(
                dag_id=dag.id,
                node_key=command.node_key,
            )
            await self._cancel_active_executions(dag.id, command.node_key, command.reason)

        await self._repository.record_audit_event(
            action=f"human_override.{command.command}",
            actor=actor,
            target_type="task_dag",
            target_id=dag.id,
            metadata={
                "external_id": command.external_id,
                "node_key": command.node_key,
                "channel": channel,
                "reason": command.reason,
                "status": node.status,
            },
        )
        return NodeOverrideResult(
            task_id=task.id,
            dag_id=dag.id,
            node_key=command.node_key,
            command=command.command,
            status=node.status,
        )

    async def _cancel_active_executions(
        self,
        dag_id: str,
        node_key: str,
        reason: str | None,
    ) -> None:
        executions = await self._repository.list_dag_node_executions(
            dag_id=dag_id,
            node_key=node_key,
        )
        for execution in executions:
            if execution.status in {"queued", "running", "needs_input"}:
                await self._repository.update_dag_node_execution(
                    execution_id=execution.id,
                    status="cancelled",
                    error=reason,
                )


def parse_human_override(text: str) -> HumanOverrideCommand | None:
    match = re.match(
        r"^/(?P<command>pause|resume|takeover|context|reject)\s+"
        r"(?P<external_id>[A-Z][A-Z0-9]+-\d+)"
        r"(?:\s+(?P<reason>.+))?$",
        text.strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    reason = match.group("reason")
    return HumanOverrideCommand(
        command=match.group("command").lower(),
        external_id=match.group("external_id"),
        reason=reason.strip() if reason else None,
    )


def parse_task_info(text: str) -> TaskInfoCommand | None:
    match = re.match(
        r"^/(?P<command>status|context|agents|nodes|running|why-blocked)\s+"
        r"(?P<external_id>[A-Z][A-Z0-9]+-\d+)\s*$",
        text.strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return TaskInfoCommand(
        command=match.group("command").lower(),
        external_id=match.group("external_id"),
    )


def parse_node_override(text: str) -> NodeOverrideCommand | None:
    match = re.match(
        r"^/(?P<command>pause-node|retry-node|skip-node)\s+"
        r"(?P<external_id>[A-Z][A-Z0-9]+-\d+)\s+"
        r"(?P<node_key>[A-Za-z0-9_.-]+)"
        r"(?:\s+(?P<reason>.+))?$",
        text.strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    reason = match.group("reason")
    return NodeOverrideCommand(
        command=match.group("command").lower(),
        external_id=match.group("external_id"),
        node_key=match.group("node_key"),
        reason=reason.strip() if reason else None,
    )


def parse_plan_approval(text: str) -> PlanApprovalCommand | None:
    match = re.match(
        r"^/approve-plan\s+"
        r"(?P<external_id>[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)+)\s*$",
        text.strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return PlanApprovalCommand(external_id=match.group("external_id"))
