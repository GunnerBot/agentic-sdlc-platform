import hmac
import json
from dataclasses import dataclass
from hashlib import sha256

from fastapi import HTTPException, status

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.glue.human_override import (
    HumanOverrideCommand,
    HumanOverrideHandler,
    parse_human_override,
)
from agentic_sdlc_platform.glue.task_event_normalizer import (
    NormalizedTaskEvent,
    TaskEventNormalizer,
)
from agentic_sdlc_platform.models.webhooks import WebhookAcceptedResponse
from agentic_sdlc_platform.persistence.repository import (
    InboundEventWriteResult,
    PersistenceRepository,
)
from agentic_sdlc_platform.ports.hermes_session import (
    HermesSessionPort,
    HermesStartSessionRequest,
)
from agentic_sdlc_platform.ports.issue_tracker import (
    IssueTrackerPort,
    IssueTrackerReply,
    IssueTrackerUpdate,
)
from agentic_sdlc_platform.ports.task_orchestrator import (
    TaskOrchestratorPort,
    TaskRequest,
    TaskUpdateRequest,
)


@dataclass(frozen=True)
class RecordedDelivery:
    inbound_event: InboundEventWriteResult
    task_id: str | None = None


class WebhookBridge:
    def __init__(
        self,
        settings: Settings,
        repository: PersistenceRepository,
        task_orchestrator: TaskOrchestratorPort | None = None,
        issue_tracker: IssueTrackerPort | None = None,
        hermes_session: HermesSessionPort | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._task_orchestrator = task_orchestrator
        self._issue_tracker = issue_tracker
        self._hermes_session = hermes_session
        self._normalizer = TaskEventNormalizer(
            linear_agent_user_id=settings.linear_agent_user_id
        )

    async def accept_linear(
        self,
        payload: bytes,
        delivery_id: str,
        signature: str | None,
    ) -> WebhookAcceptedResponse:
        self._verify_optional_hmac(
            payload=payload,
            signature=signature,
            secret=self._settings.linear_signing_secret,
            prefix=None,
        )
        result = await self._record_delivery(
            source="linear",
            delivery_id=delivery_id,
            event_type=self._extract_event_type(payload, default="unknown"),
            payload=payload,
        )
        return WebhookAcceptedResponse(
            accepted=True,
            source="linear",
            task_id=result.task_id,
            delivery_id=delivery_id,
            duplicate=not result.inbound_event.created,
        )

    async def accept_github(
        self,
        payload: bytes,
        event: str | None,
        delivery_id: str,
        signature: str | None,
    ) -> WebhookAcceptedResponse:
        if not event:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing X-GitHub-Event header",
            )

        self._verify_optional_hmac(
            payload=payload,
            signature=signature,
            secret=self._settings.github_webhook_secret,
            prefix="sha256=",
        )
        result = await self._record_delivery(
            source="github",
            delivery_id=delivery_id,
            event_type=event,
            payload=payload,
        )
        return WebhookAcceptedResponse(
            accepted=True,
            source=f"github:{event}",
            task_id=result.task_id,
            delivery_id=delivery_id,
            duplicate=not result.inbound_event.created,
        )

    async def _record_delivery(
        self,
        source: str,
        delivery_id: str,
        event_type: str,
        payload: bytes,
    ) -> RecordedDelivery:
        parsed_payload = self._parse_payload(payload)
        result = await self._repository.record_inbound_event(
            source=source,
            delivery_id=delivery_id,
            event_type=event_type,
            payload=parsed_payload,
        )
        await self._repository.record_audit_event(
            action="webhook.accepted" if result.created else "webhook.duplicate",
            actor="system",
            target_type="inbound_event",
            target_id=result.event.id,
            metadata={
                "source": source,
                "delivery_id": delivery_id,
                "event_type": event_type,
            },
        )
        if source == "linear" and event_type == "Comment":
            task_id = await self._resume_linear_session_from_comment(result, parsed_payload)
            return RecordedDelivery(inbound_event=result, task_id=task_id)
        task_id = await self._normalize_task(result, source, event_type, parsed_payload)
        if task_id is None:
            task_id = await self._update_task_from_delivery(source, event_type, parsed_payload)
        return RecordedDelivery(inbound_event=result, task_id=task_id)

    async def _resume_linear_session_from_comment(
        self,
        result: InboundEventWriteResult,
        payload: dict[str, object],
    ) -> str | None:
        if (
            not result.created
            or self._hermes_session is None
            or self._issue_tracker is None
        ):
            return None

        data = _dict_value(payload.get("data"))
        issue = _dict_value(data.get("issue"))
        issue_id = _str_value(issue.get("id"))
        comment_id = _str_value(data.get("id"))
        body = _str_value(data.get("body"))
        if not issue_id or not body:
            return None
        user_id = _str_value(_dict_value(data.get("user")).get("id"))

        agent_session = await self._repository.find_agent_session(
            provider="linear",
            external_thread_id=issue_id,
        )
        if agent_session is None or not agent_session.hermes_session_id:
            return None

        actor = f"linear:{user_id or 'unknown'}"
        if user_id and user_id == self._settings.linear_agent_user_id:
            await self._repository.record_audit_event(
                action="agent_session.self_comment_ignored",
                actor=actor,
                target_type="agent_session",
                target_id=agent_session.id,
                metadata={
                    "provider": "linear",
                    "issue_id": issue_id,
                    "comment_id": comment_id,
                },
            )
            return agent_session.task_id

        command = parse_human_override(body)
        if command is not None:
            return await self._handle_linear_comment_command(
                agent_session_id=agent_session.id,
                issue_id=issue_id,
                comment_id=comment_id,
                body=body,
                actor=actor,
                command=command,
            )

        await self._repository.record_session_event(
            session_id=agent_session.id,
            direction="inbound",
            event_type="comment",
            actor=actor,
            message=body,
            metadata={"comment_id": comment_id} if comment_id else {},
        )
        response = await self._hermes_session.resume_session(
            session_id=agent_session.hermes_session_id,
            text=body,
            actor=actor,
        )
        if response.answer:
            await self._repository.record_session_event(
                session_id=agent_session.id,
                direction="outbound",
                event_type="reply",
                actor="agent",
                message=response.answer,
                metadata={"message_id": response.message_id},
            )
            await self._issue_tracker.reply(
                IssueTrackerReply(issue_id=issue_id, body=response.answer)
            )
        await self._repository.record_audit_event(
            action="agent_session.resumed",
            actor=actor,
            target_type="agent_session",
            target_id=agent_session.id,
            metadata={
                "provider": "linear",
                "issue_id": issue_id,
                "comment_id": comment_id,
                "hermes_session_id": agent_session.hermes_session_id,
            },
        )
        return agent_session.task_id

    async def _handle_linear_comment_command(
        self,
        agent_session_id: str,
        issue_id: str,
        comment_id: str | None,
        body: str,
        actor: str,
        command: HumanOverrideCommand,
    ) -> str | None:
        await self._repository.record_session_event(
            session_id=agent_session_id,
            direction="inbound",
            event_type="command",
            actor=actor,
            message=body,
            metadata={"comment_id": comment_id} if comment_id else {},
        )
        result = await HumanOverrideHandler(
            repository=self._repository,
            task_orchestrator=self._task_orchestrator,
        ).handle(
            command=command,
            actor=actor,
            channel="linear",
        )
        reply_body = (
            f"Command /{result.command} applied. "
            f"Task {command.external_id} is now {result.status}."
        )
        await self._repository.record_session_event(
            session_id=agent_session_id,
            direction="outbound",
            event_type="command_ack",
            actor="system",
            message=reply_body,
            metadata={"command": result.command, "status": result.status},
        )
        if self._issue_tracker is not None:
            await self._issue_tracker.reply(
                IssueTrackerReply(issue_id=issue_id, body=reply_body)
            )
        return result.task_id

    async def _normalize_task(
        self,
        result: InboundEventWriteResult,
        source: str,
        event_type: str,
        payload: dict[str, object],
    ) -> str | None:
        if not result.created:
            return None

        task_event = self._normalizer.normalize(
            source=source,
            event_type=event_type,
            payload=payload,
        )
        if task_event is None:
            return None

        task = await self._repository.create_task_from_event(
            event_id=result.event.id,
            source=task_event.source,
            external_id=task_event.external_id,
            title=task_event.title,
            repo=task_event.repo,
        )
        await self._repository.record_audit_event(
            action="task.normalized",
            actor="system",
            target_type="task",
            target_id=task.id,
            metadata={
                "source": task_event.source,
                "external_id": task_event.external_id,
                "repo": task_event.repo,
            },
        )
        task_metadata: dict[str, object] | None = None
        if source == "linear" and task_event.repo:
            repo = await self._repository.get_repo_by_name(task_event.repo)
            if repo is None:
                task = await self._repository.update_task_status(
                    task_id=task.id,
                    status="blocked",
                )
                await self._repository.record_audit_event(
                    action="task.blocked_unknown_repo",
                    actor="system",
                    target_type="task",
                    target_id=task.id,
                    metadata={
                        "provider": "linear",
                        "external_id": task_event.external_id,
                        "repo": task_event.repo,
                    },
                )
                if task_event.issue_id and self._issue_tracker is not None:
                    await self._issue_tracker.reply(
                        IssueTrackerReply(
                            issue_id=task_event.issue_id,
                            body=(
                                f"Repository {task_event.repo} is not registered. "
                                f"Register it before I can work on {task_event.external_id}."
                            ),
                        )
                    )
                return task.id

            task_metadata = {
                "repo_provider": repo.provider,
                "repo_clone_url": repo.clone_url,
                "repo_default_branch": repo.default_branch,
                "repo_metadata": dict(repo.metadata_json),
            }
            await self._repository.record_audit_event(
                action="repo.resolved",
                actor="system",
                target_type="task",
                target_id=task.id,
                metadata={
                    "repo": repo.name,
                    "provider": repo.provider,
                    "default_branch": repo.default_branch,
                },
            )
        if self._task_orchestrator is not None:
            external_task = await self._task_orchestrator.create_task(
                TaskRequest(
                    source=task_event.source,
                    external_id=task_event.external_id,
                    title=task_event.title,
                    repo=task_event.repo,
                    inbound_event_id=result.event.id,
                    metadata=task_metadata,
                )
            )
            task = await self._repository.mark_task_orchestrated(
                task_id=task.id,
                orchestrator_task_id=external_task.external_task_id,
                orchestrator_status=external_task.status,
            )
            await self._repository.record_audit_event(
                action="task.orchestrated",
                actor="system",
                target_type="task",
                target_id=task.id,
                metadata={
                    "provider": self._task_orchestrator.provider,
                    "external_task_id": external_task.external_task_id,
                    "status": external_task.status,
                },
            )
        if (
            source == "linear"
            and task_event.issue_id
            and self._issue_tracker is not None
        ):
            await self._issue_tracker.mark_task_queued(
                IssueTrackerUpdate(
                    issue_id=task_event.issue_id,
                    external_id=task_event.external_id,
                    internal_task_id=task.id,
                    orchestrator_task_id=task.orchestrator_task_id,
                )
            )
            await self._repository.record_audit_event(
                action="issue_tracker.task_queued",
                actor="system",
                target_type="task",
                target_id=task.id,
                metadata={
                    "provider": "linear",
                    "issue_id": task_event.issue_id,
                    "external_id": task_event.external_id,
                },
            )
        if (
            source == "linear"
            and task_event.issue_id
            and self._hermes_session is not None
        ):
            await self._start_linear_agent_session(task_id=task.id, task_event=task_event)
        return task.id

    async def _start_linear_agent_session(
        self,
        task_id: str,
        task_event: NormalizedTaskEvent,
    ) -> None:
        text = task_event.title
        if task_event.body:
            text = f"{task_event.title}\n\n{task_event.body}"

        response = await self._hermes_session.start_session(
            HermesStartSessionRequest(
                task_id=task_id,
                provider="linear",
                external_thread_id=task_event.issue_id,
                text=text,
                repo=task_event.repo,
            )
        )
        agent_session = await self._repository.create_agent_session(
            task_id=task_id,
            provider="linear",
            external_thread_id=task_event.issue_id,
            hermes_session_id=response.session_id,
            repo=task_event.repo,
        )
        await self._repository.record_session_event(
            session_id=agent_session.id,
            direction="outbound",
            event_type="session_started",
            actor="system",
            message=text,
            metadata={"message_id": response.message_id},
        )
        await self._repository.record_audit_event(
            action="agent_session.started",
            actor="system",
            target_type="agent_session",
            target_id=agent_session.id,
            metadata={
                "provider": "linear",
                "issue_id": task_event.issue_id,
                "hermes_session_id": response.session_id,
            },
        )

    async def _update_task_from_delivery(
        self,
        source: str,
        event_type: str,
        payload: dict[str, object],
    ) -> str | None:
        task_update = self._normalizer.normalize_update(
            source=source,
            event_type=event_type,
            payload=payload,
        )
        if task_update is None:
            return None

        task = await self._repository.find_task_by_external_id(task_update.external_id)
        if task is None:
            return None

        task = await self._repository.update_task_status(task_id=task.id, status=task_update.status)
        if task.orchestrator_task_id and self._task_orchestrator is not None:
            external_task = await self._task_orchestrator.update_task(
                TaskUpdateRequest(
                    external_task_id=task.orchestrator_task_id,
                    status=task_update.status,
                    metadata={
                        "source": task_update.source,
                        "event_type": event_type,
                        "external_id": task_update.external_id,
                        **(task_update.metadata or {}),
                    },
                )
            )
            task = await self._repository.mark_task_orchestrated(
                task_id=task.id,
                orchestrator_task_id=external_task.external_task_id,
                orchestrator_status=external_task.status,
            )

        await self._repository.record_audit_event(
            action="task.updated_from_github",
            actor="system",
            target_type="task",
            target_id=task.id,
            metadata={
                "source": task_update.source,
                "event_type": event_type,
                "external_id": task_update.external_id,
                "status": task_update.status,
            },
        )
        return task.id

    def _verify_optional_hmac(
        self,
        payload: bytes,
        signature: str | None,
        secret: str | None,
        prefix: str | None,
    ) -> None:
        if not secret:
            return

        if not signature:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing webhook signature",
            )

        digest = hmac.new(secret.encode("utf-8"), payload, sha256).hexdigest()
        expected = f"{prefix or ''}{digest}"
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid webhook signature",
            )

    def _parse_payload(self, payload: bytes) -> dict[str, object]:
        if not payload:
            return {}
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError:
            return {"raw": payload.decode("utf-8", errors="replace")}
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    def _extract_event_type(self, payload: bytes, default: str) -> str:
        parsed = self._parse_payload(payload)
        event_type = parsed.get("type")
        return event_type if isinstance(event_type, str) else default


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _str_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
