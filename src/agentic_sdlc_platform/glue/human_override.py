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
        r"^/(?P<command>status|context|agents)\s+"
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
