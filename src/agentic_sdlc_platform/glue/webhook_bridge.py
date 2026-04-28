import hmac
import json
import re
from dataclasses import dataclass, replace
from hashlib import sha256

from fastapi import HTTPException, status

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.glue.dag_decomposer import DagDecomposer, Subtask
from agentic_sdlc_platform.glue.dag_execution import (
    build_dag_node_execution_metadata,
    create_or_start_execution,
)
from agentic_sdlc_platform.glue.dag_templates import build_dag_template
from agentic_sdlc_platform.glue.human_override import (
    HumanOverrideCommand,
    HumanOverrideHandler,
    TaskInfoCommand,
    parse_human_override,
    parse_task_info,
)
from agentic_sdlc_platform.glue.spec_ingestion import (
    SpecIngestionBundle,
    ingest_linear_spec,
)
from agentic_sdlc_platform.glue.task_event_normalizer import (
    NormalizedTaskEvent,
    NormalizedTaskUpdate,
    TaskEventNormalizer,
)
from agentic_sdlc_platform.glue.task_info import TaskInfoHandler
from agentic_sdlc_platform.models.webhooks import WebhookAcceptedResponse
from agentic_sdlc_platform.persistence.repository import (
    InboundEventWriteResult,
    PersistenceRepository,
)
from agentic_sdlc_platform.ports.agent_executor import AgentExecutorPort
from agentic_sdlc_platform.ports.graph_store import GraphQuery, GraphStoreError, GraphStorePort
from agentic_sdlc_platform.ports.hermes_session import (
    HermesSessionPort,
    HermesStartSessionRequest,
)
from agentic_sdlc_platform.ports.issue_tracker import (
    IssueContext,
    IssueTrackerError,
    IssueTrackerPort,
    IssueTrackerReply,
    IssueTrackerUpdate,
)
from agentic_sdlc_platform.ports.model_provider import (
    ModelProviderError,
    ModelProviderPort,
    ModelRequest,
)
from agentic_sdlc_platform.ports.task_orchestrator import (
    TaskCommentRequest,
    TaskOrchestratorPort,
    TaskRequest,
    TaskUpdateRequest,
)


@dataclass(frozen=True)
class RecordedDelivery:
    inbound_event: InboundEventWriteResult
    task_id: str | None = None


@dataclass(frozen=True)
class LinearDagPlan:
    subtasks: list[Subtask]
    strategy: str
    node_keys: list[str]
    repo_contexts: dict[str, object]
    fallback_reason: str | None = None
    model_provider: str | None = None
    model: str | None = None


class WebhookBridge:
    def __init__(
        self,
        settings: Settings,
        repository: PersistenceRepository,
        task_orchestrator: TaskOrchestratorPort | None = None,
        issue_tracker: IssueTrackerPort | None = None,
        hermes_session: HermesSessionPort | None = None,
        graph_store: GraphStorePort | None = None,
        agent_executor: AgentExecutorPort | None = None,
        model_provider: ModelProviderPort | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._task_orchestrator = task_orchestrator
        self._issue_tracker = issue_tracker
        self._hermes_session = hermes_session
        self._graph_store = graph_store
        self._agent_executor = agent_executor
        self._model_provider = model_provider
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
        if agent_session is None:
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

        info_command = parse_task_info(body)
        if info_command is not None:
            return await self._handle_linear_info_command(
                agent_session_id=agent_session.id,
                issue_id=issue_id,
                comment_id=comment_id,
                body=body,
                actor=actor,
                command=info_command,
            )

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

        clarified_task_id = await self._handle_linear_repo_clarification_comment(
            agent_session_id=agent_session.id,
            task_id=agent_session.task_id,
            issue_id=issue_id,
            comment_id=comment_id,
            body=body,
            actor=actor,
        )
        if clarified_task_id is not None:
            return clarified_task_id

        await self._repository.record_session_event(
            session_id=agent_session.id,
            direction="inbound",
            event_type="comment",
            actor=actor,
            message=body,
            metadata={"comment_id": comment_id} if comment_id else {},
        )
        if (
            agent_session.orchestrator_task_id
            and agent_session.orchestrator_issue_id
            and self._task_orchestrator is not None
            and hasattr(self._task_orchestrator, "add_comment")
        ):
            response = await self._task_orchestrator.add_comment(
                TaskCommentRequest(
                    external_task_id=agent_session.orchestrator_task_id,
                    body=body,
                    actor=actor,
                    metadata={
                        "multica_issue_id": agent_session.orchestrator_issue_id,
                        "provider": "linear",
                        "external_thread_id": issue_id,
                        "comment_id": comment_id,
                    },
                )
            )
            await self._repository.record_session_event(
                session_id=agent_session.id,
                direction="outbound",
                event_type="orchestrator_comment",
                actor="system",
                message=body,
                metadata={
                    "orchestrator_provider": agent_session.orchestrator_provider,
                    "orchestrator_task_id": agent_session.orchestrator_task_id,
                    **(response.metadata or {}),
                },
            )
            await self._repository.record_audit_event(
                action="agent_session.orchestrator_comment_added",
                actor=actor,
                target_type="agent_session",
                target_id=agent_session.id,
                metadata={
                    "provider": "linear",
                    "issue_id": issue_id,
                    "comment_id": comment_id,
                    "orchestrator_task_id": agent_session.orchestrator_task_id,
                },
            )
            return agent_session.task_id

        if self._hermes_session is None or not agent_session.hermes_session_id:
            return agent_session.task_id

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

    async def _handle_linear_info_command(
        self,
        agent_session_id: str,
        issue_id: str,
        comment_id: str | None,
        body: str,
        actor: str,
        command: TaskInfoCommand,
    ) -> str | None:
        await self._repository.record_session_event(
            session_id=agent_session_id,
            direction="inbound",
            event_type=f"{command.command}_command",
            actor=actor,
            message=body,
            metadata={"comment_id": comment_id} if comment_id else {},
        )
        result = await TaskInfoHandler(self._repository).handle(command)
        reply_body = result.answer
        task_id = result.task_id

        await self._repository.record_session_event(
            session_id=agent_session_id,
            direction="outbound",
            event_type=f"{command.command}_reply",
            actor="system",
            message=reply_body,
            metadata={"command": command.command, "external_id": command.external_id},
        )
        if self._issue_tracker is not None:
            await self._issue_tracker.reply(
                IssueTrackerReply(issue_id=issue_id, body=reply_body)
            )
        await self._repository.record_audit_event(
            action=f"agent_session.{command.command}_requested",
            actor=actor,
            target_type="agent_session",
            target_id=agent_session_id,
            metadata={"provider": "linear", "issue_id": issue_id, "task_id": task_id},
        )
        return task_id

    async def _handle_linear_repo_clarification_comment(
        self,
        agent_session_id: str,
        task_id: str,
        issue_id: str,
        comment_id: str | None,
        body: str,
        actor: str,
    ) -> str | None:
        task = await self._repository.get_task(task_id)
        if (
            task is None
            or task.status != "blocked"
            or task.repo is not None
            or task.orchestrator_task_id is not None
        ):
            return None

        registered_repos = await self._repository.list_repos(status="active")
        clarification_event = NormalizedTaskEvent(
            source="linear",
            external_id=task.external_id,
            title=task.title,
            issue_id=issue_id,
            body=body,
        )
        spec_bundle = ingest_linear_spec(
            payload={"data": {"description": body}},
            task_event=clarification_event,
            registered_repos=registered_repos,
        )
        selected_repos = spec_bundle.selected_repos if spec_bundle else ()
        if len(selected_repos) != 1:
            reply_body = _linear_repo_clarification_reply(
                external_id=task.external_id,
                registered_repo_names=[repo.name for repo in registered_repos],
                unknown_repos=list(spec_bundle.repo_scope.unknown_repos) if spec_bundle else [],
            )
            await self._repository.record_session_event(
                session_id=agent_session_id,
                direction="inbound",
                event_type="repo_clarification",
                actor=actor,
                message=body,
                metadata={"comment_id": comment_id} if comment_id else {},
            )
            await self._repository.record_session_event(
                session_id=agent_session_id,
                direction="outbound",
                event_type="repo_clarification_requested",
                actor="system",
                message=reply_body,
                metadata={"comment_id": comment_id} if comment_id else {},
            )
            if self._issue_tracker is not None:
                await self._issue_tracker.reply(
                    IssueTrackerReply(issue_id=issue_id, body=reply_body)
                )
            return task.id

        repo_name = selected_repos[0]
        repo = await self._repository.get_repo_by_name(repo_name)
        if repo is None:
            return None

        await self._repository.record_session_event(
            session_id=agent_session_id,
            direction="inbound",
            event_type="repo_clarification",
            actor=actor,
            message=body,
            metadata={
                "comment_id": comment_id,
                "resolved_repo": repo_name,
            } if comment_id else {"resolved_repo": repo_name},
        )
        task = await self._repository.update_task_repo_and_status(
            task_id=task.id,
            repo=repo_name,
            status="queued",
        )
        task_metadata: dict[str, object] = {
            "repo_provider": repo.provider,
            "repo_clone_url": repo.clone_url,
            "repo_default_branch": repo.default_branch,
            "repo_metadata": dict(repo.metadata_json),
            "repo_clarification": {
                "comment_id": comment_id,
                "actor": actor,
                "resolved_repo": repo_name,
            },
        }
        repo_context = await self._repo_context_for_task(repo.name, clarification_event)
        if repo_context is not None:
            task_metadata["repo_context"] = repo_context

        external_task_id = task.orchestrator_task_id
        external_status = task.orchestrator_status
        if self._task_orchestrator is not None:
            external_task = await self._task_orchestrator.create_task(
                TaskRequest(
                    source=task.source,
                    external_id=task.external_id,
                    title=task.title,
                    repo=repo_name,
                    metadata=task_metadata,
                )
            )
            external_task_id = external_task.external_task_id
            external_status = external_task.status
            task = await self._repository.mark_task_orchestrated(
                task_id=task.id,
                orchestrator_task_id=external_task.external_task_id,
                orchestrator_status=external_task.status,
            )
            await self._repository.create_agent_session(
                task_id=task.id,
                provider="linear",
                external_thread_id=issue_id,
                hermes_session_id=None,
                repo=repo_name,
                orchestrator_provider=self._task_orchestrator.provider,
                orchestrator_issue_id=_str_value(
                    (external_task.metadata or {}).get("multica_issue_id")
                ),
                orchestrator_task_id=external_task.external_task_id,
            )

        if self._issue_tracker is not None:
            await self._issue_tracker.mark_task_queued(
                IssueTrackerUpdate(
                    issue_id=issue_id,
                    external_id=task.external_id,
                    internal_task_id=task.id,
                    orchestrator_task_id=external_task_id,
                )
            )
            reply_body = f"Thanks, I will use {repo_name} and start {task.external_id}."
            await self._issue_tracker.reply(
                IssueTrackerReply(issue_id=issue_id, body=reply_body)
            )
            await self._repository.record_session_event(
                session_id=agent_session_id,
                direction="outbound",
                event_type="repo_clarification_resolved",
                actor="system",
                message=reply_body,
                metadata={
                    "comment_id": comment_id,
                    "resolved_repo": repo_name,
                    "orchestrator_task_id": external_task_id,
                },
            )

        await self._repository.record_audit_event(
            action="task.repo_clarification_resolved",
            actor=actor,
            target_type="task",
            target_id=task.id,
            metadata={
                "provider": "linear",
                "issue_id": issue_id,
                "comment_id": comment_id,
                "repo": repo_name,
                "orchestrator_task_id": external_task_id,
                "orchestrator_status": external_status,
            },
        )
        return task.id

    def _linear_status_reply(self, task) -> str:
        active_sessions = sum(1 for session in task.sessions if session.status == "active")
        session_word = "session" if active_sessions == 1 else "sessions"
        return (
            f"Task {task.external_id} status: {task.status}. "
            f"Orchestrator: {_orchestrator_summary(task)}. "
            f"Repo: {task.repo or 'none'}. "
            f"Sessions: {active_sessions} active {session_word}. "
            f"{_dag_progress_summary(task)}"
        )

    async def _linear_context_reply(self, task) -> str:
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

    def _linear_agents_reply(self, task) -> str:
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

    def _linear_nodes_reply(self, task) -> str:
        dags = getattr(task, "dags", [])
        if not dags:
            return f"Task {task.external_id} nodes:\n- none"
        dag = dags[0]
        node_lines = []
        for node in dag.nodes:
            depends_on = ",".join(node.depends_on) if node.depends_on else "none"
            orchestrator = node.orchestrator_task_id or "none"
            node_lines.append(
                "- "
                f"{node.node_key}: {node.status}; "
                f"repo {node.repo or 'none'}; "
                f"depends_on {depends_on}; "
                f"orchestrator {orchestrator}"
            )
        return "\n".join(
            [
                f"Task {task.external_id} nodes:",
                *node_lines,
            ]
        )

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

        if source == "linear" and task_event.issue_id:
            payload, task_event = await self._hydrate_linear_task_event(
                payload=payload,
                task_event=task_event,
                inbound_event_id=result.event.id,
            )

        registered_repos = []
        spec_bundle: SpecIngestionBundle | None = None
        effective_repo = task_event.repo
        if source == "linear":
            registered_repos = await self._repository.list_repos(status="active")
            spec_bundle = ingest_linear_spec(
                payload=payload,
                task_event=task_event,
                registered_repos=registered_repos,
            )
            if (
                effective_repo is None
                and spec_bundle is not None
                and spec_bundle.repo_scope.scope == "single_repo"
                and spec_bundle.selected_repos
            ):
                effective_repo = spec_bundle.selected_repos[0]

        task = await self._repository.create_task_from_event(
            event_id=result.event.id,
            source=task_event.source,
            external_id=task_event.external_id,
            title=task_event.title,
            repo=effective_repo,
        )
        await self._repository.record_audit_event(
            action="task.normalized",
            actor="system",
            target_type="task",
            target_id=task.id,
            metadata={
                "source": task_event.source,
                "external_id": task_event.external_id,
                "repo": effective_repo,
            },
        )
        if spec_bundle is not None:
            await self._repository.record_audit_event(
                action="task.spec_ingested",
                actor="system",
                target_type="task",
                target_id=task.id,
                metadata=spec_bundle.to_metadata(),
            )
        if (
            source == "linear"
            and task_event.issue_id
            and effective_repo is None
            and spec_bundle is not None
            and spec_bundle.repo_scope.scope in {"needs_clarification", "unspecified"}
        ):
            task = await self._repository.update_task_status(
                task_id=task.id,
                status="blocked",
            )
            agent_session = await self._repository.create_agent_session(
                task_id=task.id,
                provider="linear",
                external_thread_id=task_event.issue_id,
                hermes_session_id=None,
                repo=None,
            )
            reply_body = _linear_repo_clarification_reply(
                external_id=task_event.external_id,
                registered_repo_names=[repo.name for repo in registered_repos],
                unknown_repos=list(spec_bundle.repo_scope.unknown_repos),
            )
            await self._repository.record_session_event(
                session_id=agent_session.id,
                direction="outbound",
                event_type="repo_clarification_requested",
                actor="system",
                message=reply_body,
                metadata={"spec_ingestion": spec_bundle.to_metadata()},
            )
            if self._issue_tracker is not None:
                await self._issue_tracker.reply(
                    IssueTrackerReply(issue_id=task_event.issue_id, body=reply_body)
                )
            await self._repository.record_audit_event(
                action="task.blocked_repo_clarification",
                actor="system",
                target_type="task",
                target_id=task.id,
                metadata={
                    "provider": "linear",
                    "external_id": task_event.external_id,
                    "repo_scope": spec_bundle.repo_scope.to_metadata(),
                },
            )
            return task.id
        task_metadata: dict[str, object] | None = None
        repo_name: str | None = None
        dag_template: str | None = None
        first_dag_node: str | None = None
        first_dag_node_status: str | None = None
        planned_node_keys: list[str] = []
        if source == "linear" and effective_repo:
            repo = await self._repository.get_repo_by_name(effective_repo)
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
                        "repo": effective_repo,
                    },
                )
                if task_event.issue_id and self._issue_tracker is not None:
                    await self._issue_tracker.reply(
                        IssueTrackerReply(
                            issue_id=task_event.issue_id,
                            body=(
                                f"Repository {effective_repo} is not registered. "
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
            repo_context = await self._repo_context_for_task(repo.name, task_event)
            if repo_context is not None:
                task_metadata["repo_context"] = repo_context
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
            repo_name = repo.name
        if spec_bundle is not None:
            if task_metadata is None:
                task_metadata = {}
            task_metadata["spec_ingestion"] = spec_bundle.to_metadata()
        if self._task_orchestrator is not None:
            external_task = await self._task_orchestrator.create_task(
                TaskRequest(
                    source=task_event.source,
                    external_id=task_event.external_id,
                    title=task_event.title,
                    repo=effective_repo,
                    inbound_event_id=result.event.id,
                    metadata=task_metadata,
                )
            )
            task = await self._repository.mark_task_orchestrated(
                task_id=task.id,
                orchestrator_task_id=external_task.external_task_id,
                orchestrator_status=external_task.status,
            )
            if source == "linear" and task_event.issue_id:
                await self._repository.create_agent_session(
                    task_id=task.id,
                    provider="linear",
                    external_thread_id=task_event.issue_id,
                    hermes_session_id=None,
                    repo=effective_repo,
                    orchestrator_provider=self._task_orchestrator.provider,
                    orchestrator_issue_id=_str_value(
                        (external_task.metadata or {}).get("multica_issue_id")
                    ),
                    orchestrator_task_id=external_task.external_task_id,
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
            await self._start_linear_agent_session(
                task_id=task.id,
                task_event=task_event,
                repo=effective_repo,
                spec_bundle=spec_bundle,
            )
        if source == "linear" and task_event.dag_template:
            dag_template = task_event.dag_template
            dag = await self._repository.create_task_dag(
                task_id=task.id,
                subtasks=build_dag_template(task_event.dag_template, task),
            )
            await self._repository.record_audit_event(
                action="task.dag_template_created",
                actor="system",
                target_type="task",
                target_id=task.id,
                metadata={
                    "template": task_event.dag_template,
                    "dag_id": dag.id,
                    "node_count": len(dag.nodes),
                },
            )
            if self._task_orchestrator is not None:
                first_dag_node, first_dag_node_status = await self._enqueue_first_ready_dag_node(
                    dag=dag,
                    task=task,
                )
        elif (
            source == "linear"
            and spec_bundle is not None
            and (
                spec_bundle.repo_scope.scope == "multi_repo"
                or (
                    spec_bundle.repo_scope.scope == "single_repo"
                    and self._model_planning_configured()
                )
            )
        ):
            dag_template = "linear-spec"
            dag_plan = await self._plan_linear_spec_dag(
                task_event=task_event,
                spec_bundle=spec_bundle,
                registered_repos=registered_repos,
            )
            dag = await self._repository.create_task_dag(
                task_id=task.id,
                subtasks=dag_plan.subtasks,
            )
            planned_node_keys = [node.node_key for node in dag.nodes]
            await self._repository.record_audit_event(
                action="task.dag_planned",
                actor="system",
                target_type="task",
                target_id=task.id,
                metadata={
                    "template": dag_template,
                    "strategy": dag_plan.strategy,
                    "fallback_reason": dag_plan.fallback_reason,
                    "node_count": len(dag.nodes),
                    "node_keys": planned_node_keys,
                    "repo_contexts": dag_plan.repo_contexts,
                    "model_provider": dag_plan.model_provider,
                    "model": dag_plan.model,
                },
            )
            await self._repository.record_audit_event(
                action="task.dag_template_created",
                actor="system",
                target_type="task",
                target_id=task.id,
                metadata={
                    "template": dag_template,
                    "dag_id": dag.id,
                    "node_count": len(dag.nodes),
                    "repo_scope": spec_bundle.repo_scope.to_metadata(),
                    "planning_strategy": dag_plan.strategy,
                },
            )
            if self._task_orchestrator is not None:
                ready_nodes = [
                    node
                    for node in dag.nodes
                    if node.status == "ready" and not node.depends_on
                ]
                queued_nodes = await self._enqueue_ready_dag_nodes(
                    dag=dag,
                    task=task,
                    ready_nodes=ready_nodes,
                    extra_metadata={
                        "spec_ingestion": spec_bundle.to_metadata(),
                        "planning_strategy": dag_plan.strategy,
                    },
                )
                if queued_nodes:
                    first_dag_node = queued_nodes[0].node_key
                    first_dag_node_status = queued_nodes[0].status
        if source == "linear" and task_event.issue_id and self._issue_tracker is not None:
            reply_body = _linear_assignment_reply(
                external_id=task_event.external_id,
                repo=repo_name or effective_repo,
                dag_template=dag_template,
                first_dag_node=first_dag_node,
                first_dag_node_status=first_dag_node_status,
                spec_bundle=spec_bundle,
                planned_node_keys=planned_node_keys,
            )
            await self._issue_tracker.reply(
                IssueTrackerReply(issue_id=task_event.issue_id, body=reply_body)
            )
            await self._repository.record_audit_event(
                action="issue_tracker.assignment_acknowledged",
                actor="system",
                target_type="task",
                target_id=task.id,
                metadata={
                    "provider": "linear",
                    "issue_id": task_event.issue_id,
                    "external_id": task_event.external_id,
                    "repo": repo_name or effective_repo,
                    "dag_template": dag_template,
                    "first_dag_node": first_dag_node,
                    "first_dag_node_status": first_dag_node_status,
                    "spec_ingestion": spec_bundle.to_metadata() if spec_bundle else None,
                },
            )
        return task.id

    async def _hydrate_linear_task_event(
        self,
        payload: dict[str, object],
        task_event: NormalizedTaskEvent,
        inbound_event_id: str,
    ) -> tuple[dict[str, object], NormalizedTaskEvent]:
        if self._issue_tracker is None or not hasattr(self._issue_tracker, "get_issue_context"):
            return payload, task_event

        try:
            issue_context = await self._issue_tracker.get_issue_context(task_event.issue_id)
        except (IssueTrackerError, KeyError):
            await self._repository.record_audit_event(
                action="linear.issue_hydration_failed",
                actor="system",
                target_type="inbound_event",
                target_id=inbound_event_id,
                metadata={
                    "issue_id": task_event.issue_id,
                    "external_id": task_event.external_id,
                },
            )
            return payload, task_event

        hydrated_payload = _merge_linear_issue_context(payload, issue_context)
        hydrated_event = task_event
        if issue_context.description:
            hydrated_event = replace(hydrated_event, body=issue_context.description)
        if issue_context.url:
            hydrated_event = replace(hydrated_event, url=issue_context.url)
        if issue_context.title and not hydrated_event.title:
            hydrated_event = replace(hydrated_event, title=issue_context.title)
        await self._repository.record_audit_event(
            action="linear.issue_hydrated",
            actor="system",
            target_type="inbound_event",
            target_id=inbound_event_id,
            metadata={
                "issue_id": task_event.issue_id,
                "external_id": task_event.external_id,
                "attachment_count": len(issue_context.attachments or []),
                "comment_count": len(issue_context.comments or []),
                "has_description": bool(issue_context.description),
            },
        )
        return hydrated_payload, hydrated_event

    async def _plan_linear_spec_dag(
        self,
        task_event: NormalizedTaskEvent,
        spec_bundle: SpecIngestionBundle,
        registered_repos: list[object],
    ) -> LinearDagPlan:
        repo_names = list(spec_bundle.selected_repos)
        repo_contexts: dict[str, object] = {}
        fallback_reason: str | None = None
        model_provider: str | None = None
        model: str | None = None
        planned_subtasks: list[Subtask] = []

        if self._model_planning_configured():
            repo_contexts = await self._linear_planner_repo_contexts(
                task_event=task_event,
                spec_bundle=spec_bundle,
                repo_names=repo_names,
            )
            prompt = _linear_spec_planner_prompt(
                task_event=task_event,
                spec_bundle=spec_bundle,
                repo_contexts=repo_contexts,
            )
            try:
                response = await self._model_provider.complete(
                    ModelRequest(
                        role="plan_agent",
                        prompt=prompt,
                        metadata={"source": "linear", "external_id": task_event.external_id},
                    )
                )
                model_provider = response.provider
                model = response.model
                parsed_subtasks = DagDecomposer().parse_subtasks(response.content)
                planned_subtasks = _valid_planned_subtasks(
                    subtasks=parsed_subtasks,
                    allowed_repos=set(repo_names),
                )
                if not planned_subtasks:
                    fallback_reason = "invalid_model_plan"
            except ModelProviderError:
                fallback_reason = "model_provider_error"

        if planned_subtasks:
            return LinearDagPlan(
                subtasks=planned_subtasks,
                strategy="model",
                node_keys=[subtask.id for subtask in planned_subtasks],
                repo_contexts=repo_contexts,
                model_provider=model_provider,
                model=model,
            )

        fallback_subtasks = spec_bundle.to_repo_subtasks()
        return LinearDagPlan(
            subtasks=fallback_subtasks,
            strategy="repo_fallback",
            node_keys=[subtask.id for subtask in fallback_subtasks],
            repo_contexts=repo_contexts,
            fallback_reason=fallback_reason or "model_planning_unavailable",
            model_provider=model_provider,
            model=model,
        )

    def _model_planning_configured(self) -> bool:
        if (
            not self._settings.linear_spec_planner_enabled
            or self._model_provider is None
            or not self._settings.vendor_http_enabled
        ):
            return False
        if self._settings.model_provider == "openai":
            return bool(self._settings.openai_api_key)
        if self._settings.model_provider == "claude":
            return bool(self._settings.claude_api_key)
        return True

    async def _linear_planner_repo_contexts(
        self,
        task_event: NormalizedTaskEvent,
        spec_bundle: SpecIngestionBundle,
        repo_names: list[str],
    ) -> dict[str, object]:
        contexts: dict[str, object] = {}
        if self._graph_store is None or not self._settings.vendor_http_enabled:
            return contexts
        question = _linear_spec_planning_question(task_event, spec_bundle)
        for repo_name in repo_names:
            try:
                result = await self._graph_store.query(
                    GraphQuery(
                        repo=repo_name,
                        question=question,
                        metadata={
                            "source": task_event.source,
                            "external_id": task_event.external_id,
                            "purpose": "linear_spec_planning",
                        },
                    )
                )
            except GraphStoreError as exc:
                contexts[repo_name] = {"status": "unavailable", "reason": str(exc)}
                continue
            contexts[repo_name] = {
                "status": "available",
                "provider": result.provider,
                "answer": result.answer,
                "references": result.references,
            }
        return contexts

    async def _repo_context_for_task(
        self,
        repo: str,
        task_event: NormalizedTaskEvent,
    ) -> dict[str, object] | None:
        if (
            self._graph_store is None
            or not self._settings.vendor_http_enabled
        ):
            return None
        question = task_event.title
        if task_event.body:
            question = f"{task_event.title}\n\n{task_event.body}"
        try:
            result = await self._graph_store.query(
                GraphQuery(
                    repo=repo,
                    question=question,
                    metadata={
                        "source": task_event.source,
                        "external_id": task_event.external_id,
                    },
                )
            )
        except GraphStoreError as exc:
            return {"status": "unavailable", "reason": str(exc)}
        return {
            "status": "available",
            "provider": result.provider,
            "answer": result.answer,
            "references": result.references,
        }

    async def _enqueue_first_ready_dag_node(
        self,
        dag,
        task,
    ) -> tuple[str, str] | tuple[None, None]:
        ready_nodes = [
            node
            for node in dag.nodes
            if node.status == "ready" and not node.depends_on
        ]
        if not ready_nodes:
            return None, None
        node = ready_nodes[0]
        queued_nodes = await self._enqueue_ready_dag_nodes(
            dag=dag,
            task=task,
            ready_nodes=[node],
        )
        queued_node = queued_nodes[0]
        return queued_node.node_key, queued_node.status

    async def _enqueue_ready_dag_nodes(
        self,
        dag,
        task,
        ready_nodes,
        extra_metadata: dict[str, object] | None = None,
    ):
        queued_nodes = []
        if self._task_orchestrator is None:
            return queued_nodes
        for node in ready_nodes:
            metadata = await build_dag_node_execution_metadata(
                dag=dag,
                task=task,
                node=node,
                repository=self._repository,
                graph_store=self._graph_store,
            )
            if extra_metadata:
                metadata.update(extra_metadata)
            external_task = await self._task_orchestrator.create_task(
                TaskRequest(
                    source="dag",
                    external_id=f"{dag.id}:{node.node_key}",
                    title=node.title,
                    repo=node.repo,
                    metadata=metadata,
                )
            )
            persisted_metadata = {
                **metadata,
                **(external_task.metadata or {}),
            }
            queued_node = await self._repository.mark_dag_node_orchestrated(
                dag_id=dag.id,
                node_key=node.node_key,
                orchestrator_task_id=external_task.external_task_id,
                orchestrator_status=external_task.status,
                metadata=persisted_metadata,
            )
            queued_nodes.append(queued_node)
            await create_or_start_execution(
                repository=self._repository,
                agent_executor=self._agent_executor,
                dag=dag,
                task=task,
                node=node,
                metadata=persisted_metadata,
            )
            await self._repository.record_audit_event(
                action="task.dag_node_enqueued",
                actor="system",
                target_type="task_dag",
                target_id=dag.id,
                metadata={
                    "task_id": task.id,
                    "external_id": task.external_id,
                    "node_key": node.node_key,
                    "orchestrator_task_id": external_task.external_task_id,
                    "status": external_task.status,
                },
            )
        return queued_nodes

    async def _start_linear_agent_session(
        self,
        task_id: str,
        task_event: NormalizedTaskEvent,
        repo: str | None = None,
        spec_bundle: SpecIngestionBundle | None = None,
    ) -> None:
        text = task_event.title
        if task_event.body:
            text = f"{task_event.title}\n\n{task_event.body}"
        if spec_bundle is not None:
            text = f"{text}\n{spec_bundle.prompt_suffix()}"

        response = await self._hermes_session.start_session(
            HermesStartSessionRequest(
                task_id=task_id,
                provider="linear",
                external_thread_id=task_event.issue_id,
                text=text,
                repo=repo,
            )
        )
        agent_session = await self._repository.create_agent_session(
            task_id=task_id,
            provider="linear",
            external_thread_id=task_event.issue_id,
            hermes_session_id=response.session_id,
            repo=repo,
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

        if task_update.dag_id and task_update.dag_node_key:
            task_id = await self._update_dag_node_from_delivery(
                task_update=task_update,
                event_type=event_type,
            )
            if task_id is not None:
                return task_id

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

    async def _update_dag_node_from_delivery(
        self,
        task_update: NormalizedTaskUpdate,
        event_type: str,
    ) -> str | None:
        if not task_update.dag_id or not task_update.dag_node_key:
            return None

        dag = await self._repository.get_task_dag(task_update.dag_id)
        if dag is None:
            return None
        node = next(
            (node for node in dag.nodes if node.node_key == task_update.dag_node_key),
            None,
        )
        if node is None:
            return None

        if node.status == "completed" and task_update.status == "merged":
            await self._repository.update_dag_node_metadata(
                dag_id=task_update.dag_id,
                node_key=task_update.dag_node_key,
                metadata=_pr_node_metadata(task_update),
            )
            await self._update_latest_execution_from_pr(task_update, "completed")
            return dag.task_id

        orchestration_status = task_update.status
        node_status = task_update.status
        if task_update.status == "merged":
            orchestration_status = "completed"
            node_status = "completed"

        if node.orchestrator_task_id and self._task_orchestrator is not None:
            external_task = await self._task_orchestrator.update_task(
                TaskUpdateRequest(
                    external_task_id=node.orchestrator_task_id,
                    status=orchestration_status,
                    metadata={
                        "source": task_update.source,
                        "event_type": event_type,
                        "external_id": task_update.external_id,
                        "dag_id": task_update.dag_id,
                        "node_key": task_update.dag_node_key,
                        **(dict(node.metadata_json) if node.metadata_json else {}),
                        **(task_update.metadata or {}),
                    },
                )
            )
            orchestration_status = external_task.status

        if node_status == "completed":
            await self._repository.mark_dag_node_completed(
                dag_id=task_update.dag_id,
                node_key=task_update.dag_node_key,
                orchestrator_status=orchestration_status,
            )
            await self._repository.update_dag_node_metadata(
                dag_id=task_update.dag_id,
                node_key=task_update.dag_node_key,
                metadata=_pr_node_metadata(task_update),
            )
            await self._update_latest_execution_from_pr(task_update, "completed")
            dag = await self._repository.get_task_dag(task_update.dag_id)
            if dag is None:
                return None
            ready_nodes = await self._repository.list_ready_dag_nodes_for_dag(
                task_update.dag_id
            )
            if self._task_orchestrator is not None:
                await self._enqueue_ready_dag_nodes(
                    dag=dag,
                    task=dag.task,
                    ready_nodes=ready_nodes,
                )
        else:
            await self._repository.update_dag_node_status(
                dag_id=task_update.dag_id,
                node_key=task_update.dag_node_key,
                status=node_status,
                orchestrator_status=orchestration_status,
                metadata=_pr_node_metadata(task_update),
            )
            await self._update_latest_execution_from_pr(task_update, node_status)

        await self._repository.record_audit_event(
            action="task.dag_node_updated_from_github",
            actor="system",
            target_type="task_dag",
            target_id=task_update.dag_id,
            metadata={
                "source": task_update.source,
                "event_type": event_type,
                "external_id": task_update.external_id,
                "status": task_update.status,
                "node_status": node_status,
                "node_key": task_update.dag_node_key,
                **(task_update.metadata or {}),
            },
        )
        return dag.task_id

    async def _update_latest_execution_from_pr(
        self,
        task_update: NormalizedTaskUpdate,
        status: str,
    ) -> None:
        if not task_update.dag_id or not task_update.dag_node_key:
            return
        executions = await self._repository.list_dag_node_executions(
            dag_id=task_update.dag_id,
            node_key=task_update.dag_node_key,
        )
        if not executions:
            return
        latest = executions[0]
        metadata = _pr_node_metadata(task_update)
        await self._repository.update_dag_node_execution(
            execution_id=latest.id,
            status=status,
            pr_url=_str_or_none(metadata.get("pr_url")),
            pr_number=_int_or_none(metadata.get("pr_number")),
            metadata=metadata,
        )

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


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _single_line(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.split())


def _orchestrator_summary(task) -> str:
    if not task.orchestrator_task_id:
        return "none"
    status = task.orchestrator_status or "unknown"
    return f"{task.orchestrator_task_id} ({status})"


def _dag_progress_summary(task) -> str:
    dags = getattr(task, "dags", [])
    if not dags:
        return "DAG: none."
    dag = dags[0]
    completed = {node.node_key for node in dag.nodes if node.status == "completed"}
    ready_nodes = [
        node
        for node in dag.nodes
        if node.status != "completed"
        and all(dependency in completed for dependency in node.depends_on)
    ]
    next_node = ready_nodes[0].node_key if ready_nodes else "none"
    return (
        f"DAG: {dag.status}, {len(completed)}/{len(dag.nodes)} completed, "
        f"{len(ready_nodes)} ready, next: {next_node}."
    )


def _pr_node_metadata(task_update: NormalizedTaskUpdate) -> dict[str, object]:
    metadata = task_update.metadata or {}
    pr_metadata: dict[str, object] = {
        "pr_state": task_update.status,
    }
    pull_request = metadata.get("pull_request")
    if isinstance(pull_request, int):
        pr_metadata["pr_number"] = pull_request
    url = metadata.get("url")
    if isinstance(url, str):
        pr_metadata["pr_url"] = url
    if task_update.repo:
        pr_metadata["pr_repo"] = task_update.repo
    if task_update.status == "merged":
        pr_metadata["pr_state"] = "merged"
    return pr_metadata


def _linear_assignment_reply(
    external_id: str,
    repo: str | None,
    dag_template: str | None,
    first_dag_node: str | None,
    first_dag_node_status: str | None,
    spec_bundle: SpecIngestionBundle | None = None,
    planned_node_keys: list[str] | None = None,
) -> str:
    lines = [
        f"Accepted {external_id}.",
        f"Repo: {repo or 'none'}.",
    ]
    if spec_bundle is not None:
        repos = ", ".join(spec_bundle.selected_repos) or "none"
        lines.append(f"Spec repo scope: {spec_bundle.repo_scope.scope} ({repos}).")
        if spec_bundle.design_assets:
            lines.append(f"Design assets: {len(spec_bundle.design_assets)}.")
    if dag_template:
        lines.append(f"DAG template: {dag_template}.")
    else:
        lines.append("DAG template: none.")
    if planned_node_keys:
        lines.append(
            f"Planned nodes: {len(planned_node_keys)} ({', '.join(planned_node_keys)})."
        )
    if first_dag_node:
        lines.append(f"First DAG node queued: {first_dag_node} ({first_dag_node_status}).")
    else:
        lines.append("First DAG node queued: none.")
    lines.append(f"Commands: /status {external_id}, /context {external_id}, /agents {external_id}.")
    return "\n".join(lines)


def _linear_repo_clarification_reply(
    external_id: str,
    registered_repo_names: list[str],
    unknown_repos: list[str],
) -> str:
    lines = [f"I need a registered repository before I can start {external_id}."]
    if registered_repo_names:
        lines.append(f"Mention one of: {', '.join(sorted(registered_repo_names))}.")
    else:
        lines.append("No repositories are registered yet.")
    if unknown_repos:
        lines.append(f"Unregistered repo mentions: {', '.join(sorted(unknown_repos))}.")
    return "\n".join(lines)


def _linear_spec_planner_prompt(
    task_event: NormalizedTaskEvent,
    spec_bundle: SpecIngestionBundle,
    repo_contexts: dict[str, object],
) -> str:
    return "\n".join(
        [
            "Create a reviewable implementation DAG for this Linear ticket.",
            "Return only JSON: an array of objects with id, title, repo, and depends_on.",
            "Every id must be snake_case and unique.",
            "Every code node must name exactly one repo from the allowed repos.",
            "Use multiple nodes in the same repo when that makes PRs easier to review.",
            "Use dependencies when one PR should land before another.",
            f"Ticket: {task_event.external_id} - {task_event.title}",
            f"Allowed repos: {', '.join(spec_bundle.selected_repos)}",
            "Spec:",
            _truncated_spec_text(spec_bundle),
            "Design assets:",
            json.dumps(spec_bundle.to_metadata()["design_assets"], sort_keys=True),
            "GraphStore planning context:",
            json.dumps(repo_contexts, sort_keys=True),
        ]
    )


def _linear_spec_planning_question(
    task_event: NormalizedTaskEvent,
    spec_bundle: SpecIngestionBundle,
) -> str:
    return (
        "What code areas, contracts, tests, and dependencies are relevant for planning "
        f"this Linear task?\n\n{task_event.title}\n\n{_truncated_spec_text(spec_bundle)}"
    )


def _truncated_spec_text(spec_bundle: SpecIngestionBundle, limit: int = 6000) -> str:
    text = "\n\n".join(
        f"# {source.title}\n{source.text}"
        for source in spec_bundle.text_sources
    )
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n...[truncated]"


def _valid_planned_subtasks(
    subtasks: list[Subtask],
    allowed_repos: set[str],
) -> list[Subtask]:
    if not subtasks:
        return []
    node_ids = [subtask.id for subtask in subtasks]
    if len(set(node_ids)) != len(node_ids):
        return []
    if any(not _valid_node_key(subtask.id) for subtask in subtasks):
        return []
    if any(subtask.repo not in allowed_repos for subtask in subtasks):
        return []
    node_id_set = set(node_ids)
    for subtask in subtasks:
        if any(dependency not in node_id_set for dependency in subtask.depends_on):
            return []
        if subtask.id in subtask.depends_on:
            return []
    return subtasks


def _valid_node_key(value: str) -> bool:
    return re.fullmatch(r"[a-z][a-z0-9_]{1,63}", value) is not None


def _merge_linear_issue_context(
    payload: dict[str, object],
    issue_context: IssueContext,
) -> dict[str, object]:
    merged = dict(payload)
    data = dict(_dict_value(merged.get("data")))
    if issue_context.title:
        data["title"] = issue_context.title
    if issue_context.identifier:
        data["identifier"] = issue_context.identifier
    if issue_context.description:
        data["description"] = issue_context.description
    if issue_context.url:
        data["url"] = issue_context.url
    if issue_context.attachments:
        data["attachments"] = {
            "nodes": [
                _linear_attachment_payload(attachment)
                for attachment in issue_context.attachments
            ]
        }
    if issue_context.comments:
        data["comments"] = {
            "nodes": [
                _linear_comment_payload(comment)
                for comment in issue_context.comments
            ]
        }
    merged["data"] = data
    return merged


def _linear_attachment_payload(attachment) -> dict[str, object]:
    payload: dict[str, object] = {}
    if attachment.id:
        payload["id"] = attachment.id
    if attachment.title:
        payload["title"] = attachment.title
    if attachment.url:
        payload["url"] = attachment.url
    if attachment.content_type:
        payload["contentType"] = attachment.content_type
    if attachment.content:
        payload["content"] = attachment.content
    if attachment.metadata:
        payload["metadata"] = attachment.metadata
    return payload


def _linear_comment_payload(comment) -> dict[str, object]:
    payload: dict[str, object] = {}
    if comment.id:
        payload["id"] = comment.id
    if comment.body:
        payload["body"] = comment.body
    if comment.actor:
        payload["user"] = {"id": comment.actor}
    return payload
