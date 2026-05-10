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
from agentic_sdlc_platform.glue.execution_policy import (
    PLANNING_ONLY,
    bounded_graph_context,
    execution_policy_metadata,
    normalize_execution_mode,
    sanitize_write_metadata,
)
from agentic_sdlc_platform.glue.human_override import (
    HumanOverrideCommand,
    HumanOverrideHandler,
    PlanRevisionCommand,
    TaskInfoCommand,
    parse_human_override,
    parse_plan_approval,
    parse_plan_revision,
    parse_task_info,
)
from agentic_sdlc_platform.glue.llm_observability import record_llm_cost_ledger
from agentic_sdlc_platform.glue.quality_gate import (
    evaluate_completion_quality_gate,
    quality_gate_metadata,
)
from agentic_sdlc_platform.glue.runtime_repo_sync import (
    sync_runtime_repositories_for_execution,
)
from agentic_sdlc_platform.glue.spec_ingestion import (
    DesignAsset,
    RepoMatch,
    RepoScope,
    SpecIngestionBundle,
    TextSource,
    ingest_linear_spec,
    linear_design_references,
    linear_document_urls,
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
from agentic_sdlc_platform.ports.design_context import DesignContextError, DesignContextPort
from agentic_sdlc_platform.ports.document_context import (
    DocumentContextError,
    DocumentContextPort,
)
from agentic_sdlc_platform.ports.graph_store import GraphQuery, GraphStoreError, GraphStorePort
from agentic_sdlc_platform.ports.hermes_session import (
    HermesSessionError,
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
from agentic_sdlc_platform.ports.runtime_repo_registry import (
    RuntimeRepoRegistryError,
    RuntimeRepoRegistryPort,
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
    validation_error: str | None = None
    validation_errors: list[str] | None = None
    planner_attempts: int = 0
    validation_node_added: bool = False
    node_quality_gates_enabled: bool = True
    planning_failed: bool = False
    failure_message: str | None = None
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
        document_context: DocumentContextPort | None = None,
        design_context: DesignContextPort | None = None,
        agent_executor: AgentExecutorPort | None = None,
        model_provider: ModelProviderPort | None = None,
        runtime_repo_registry: RuntimeRepoRegistryPort | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._task_orchestrator = task_orchestrator
        self._issue_tracker = issue_tracker
        self._hermes_session = hermes_session
        self._graph_store = graph_store
        self._document_context = document_context
        self._design_context = design_context
        self._agent_executor = agent_executor
        self._model_provider = model_provider
        self._runtime_repo_registry = runtime_repo_registry
        self._normalizer = TaskEventNormalizer(
            linear_agent_user_id=settings.linear_agent_user_id,
            default_execution_mode=settings.agent_default_execution_mode,
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
        if not result.created:
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

        approval_command = parse_plan_approval(body)
        if approval_command is not None:
            return await self._handle_linear_plan_approval_command(
                agent_session_id=agent_session.id,
                issue_id=issue_id,
                comment_id=comment_id,
                body=body,
                actor=actor,
                external_id=approval_command.external_id,
            )

        revision_command = parse_plan_revision(body)
        if revision_command is not None:
            return await self._handle_linear_plan_revision_command(
                agent_session_id=agent_session.id,
                issue_id=issue_id,
                comment_id=comment_id,
                body=body,
                actor=actor,
                command=revision_command,
            )

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
        task = await self._repository.get_task(task.id) or task
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

        restored = _restored_linear_spec_from_task(task)
        if restored is not None:
            restored_event, restored_bundle, hydrated_spec_artifact_id = restored
            dag_task_event = replace(
                restored_event,
                issue_id=restored_event.issue_id or issue_id,
                repo=repo_name,
            )
            dag_spec_bundle = _linear_spec_bundle_with_resolved_repo(
                restored_bundle,
                repo_name,
            )
        else:
            hydrated_spec_artifact_id = None
            dag_task_event = replace(clarification_event, repo=repo_name)
            dag_spec_bundle = _linear_spec_bundle_with_resolved_repo(
                spec_bundle,
                repo_name,
            )
        if self._linear_spec_dag_requested(dag_spec_bundle):
            dag_plan = await self._plan_linear_spec_dag(
                task_id=task.id,
                task_event=dag_task_event,
                spec_bundle=dag_spec_bundle,
                registered_repos=registered_repos,
            )
            if dag_plan.planning_failed or not dag_plan.subtasks:
                task = await self._repository.update_task_status(
                    task_id=task.id,
                    status="blocked",
                )
                reply_body = _linear_planning_failed_reply(
                    external_id=task.external_id,
                    dag_plan=dag_plan,
                )
                if self._issue_tracker is not None:
                    await self._issue_tracker.reply(
                        IssueTrackerReply(issue_id=issue_id, body=reply_body)
                    )
                return task.id

            dag = await self._repository.create_task_dag(
                task_id=task.id,
                subtasks=dag_plan.subtasks,
            )
            node_metadata = _linear_spec_node_metadata(
                task_event=dag_task_event,
                spec_bundle=dag_spec_bundle,
                dag_plan=dag_plan,
                hydrated_spec_artifact_id=hydrated_spec_artifact_id,
            )
            node_metadata["repo_clarification"] = {
                "comment_id": comment_id,
                "actor": actor,
                "resolved_repo": repo_name,
            }
            for node in dag.nodes:
                await self._repository.update_dag_node_metadata(
                    dag_id=dag.id,
                    node_key=node.node_key,
                    metadata=node_metadata,
                )

            planned_node_keys = [node.node_key for node in dag.nodes]
            plan_approval_required = self._settings.linear_plan_approval_required
            first_dag_node: str | None = None
            first_dag_node_status: str | None = None
            if plan_approval_required:
                task = await self._repository.update_task_status(
                    task_id=task.id,
                    status="needs_plan_approval",
                )
            elif self._task_orchestrator is not None:
                ready_nodes = [
                    node
                    for node in dag.nodes
                    if node.status == "ready" and not node.depends_on
                ]
                queued_nodes = await self._enqueue_ready_dag_nodes(
                    dag=dag,
                    task=task,
                    ready_nodes=ready_nodes,
                )
                if queued_nodes:
                    first_dag_node = queued_nodes[0].node_key
                    first_dag_node_status = queued_nodes[0].status

            reply_body = _linear_assignment_reply(
                external_id=task.external_id,
                repo=repo_name,
                dag_template="linear-spec",
                first_dag_node=first_dag_node,
                first_dag_node_status=first_dag_node_status,
                spec_bundle=dag_spec_bundle,
                dag_plan=dag_plan,
                dag_id=dag.id,
                planned_node_keys=planned_node_keys,
                plan_approval_required=plan_approval_required,
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
                    "dag_id": dag.id,
                    "node_keys": planned_node_keys,
                },
            )
            if self._issue_tracker is not None:
                await self._issue_tracker.reply(
                    IssueTrackerReply(issue_id=issue_id, body=reply_body)
                )
            await self._repository.record_audit_event(
                action="task.repo_clarification_resolved_to_dag",
                actor=actor,
                target_type="task",
                target_id=task.id,
                metadata={
                    "provider": "linear",
                    "issue_id": issue_id,
                    "comment_id": comment_id,
                    "repo": repo_name,
                    "dag_id": dag.id,
                    "node_keys": planned_node_keys,
                    "planning_strategy": dag_plan.strategy,
                },
            )
            return task.id

        external_task_id = task.orchestrator_task_id
        external_status = task.orchestrator_status
        if self._task_orchestrator is not None:
            runtime_sync = await sync_runtime_repositories_for_execution(
                repository=self._repository,
                runtime_repo_registry=self._runtime_repo_registry,
                requested_repo=repo_name,
            )
            if runtime_sync is not None:
                task_metadata["runtime_repo_sync"] = {
                    "provider": runtime_sync.provider,
                    "workspace_id": runtime_sync.workspace_id,
                    "repo_count": runtime_sync.repo_count,
                    "urls": list(runtime_sync.urls),
                }
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

    async def _handle_linear_plan_approval_command(
        self,
        agent_session_id: str,
        issue_id: str,
        comment_id: str | None,
        body: str,
        actor: str,
        external_id: str,
    ) -> str | None:
        await self._repository.record_session_event(
            session_id=agent_session_id,
            direction="inbound",
            event_type="approve_plan_command",
            actor=actor,
            message=body,
            metadata={"comment_id": comment_id} if comment_id else {},
        )
        task = await self._repository.find_task_by_external_id(external_id)
        if task is None or not getattr(task, "dags", []):
            reply_body = f"Task {external_id} has no plan to approve."
            if self._issue_tracker is not None:
                await self._issue_tracker.reply(
                    IssueTrackerReply(issue_id=issue_id, body=reply_body)
                )
            return None

        dag = _latest_task_dag(task)
        if dag is None:
            reply_body = f"Task {external_id} has no active plan to approve."
            if self._issue_tracker is not None:
                await self._issue_tracker.reply(
                    IssueTrackerReply(issue_id=issue_id, body=reply_body)
                )
            return task.id
        ready_nodes = await self._repository.list_ready_dag_nodes_for_dag(dag.id)
        queued_nodes = []
        if self._task_orchestrator is not None:
            dag = await self._repository.get_task_dag(dag.id)
            if dag is None:
                return task.id
            queued_nodes = await self._enqueue_ready_dag_nodes(
                dag=dag,
                task=dag.task,
                ready_nodes=ready_nodes,
                extra_metadata={
                    "plan_approved": True,
                    "plan_approved_by": actor,
                    "plan_approval_comment_id": comment_id,
                },
            )
        task = await self._repository.update_task_status(task_id=task.id, status="queued")
        queued_names = [node.node_key for node in queued_nodes]
        reply_body = (
            f"Plan approved for {external_id}. "
            f"Queued nodes: {', '.join(queued_names) if queued_names else 'none'}."
        )
        await self._repository.record_session_event(
            session_id=agent_session_id,
            direction="outbound",
            event_type="plan_approved",
            actor="system",
            message=reply_body,
            metadata={
                "comment_id": comment_id,
                "queued_nodes": queued_names,
                "dag_id": dag.id,
            },
        )
        if self._issue_tracker is not None:
            await self._issue_tracker.reply(
                IssueTrackerReply(issue_id=issue_id, body=reply_body)
            )
        await self._repository.record_audit_event(
            action="task.plan_approved",
            actor=actor,
            target_type="task",
            target_id=task.id,
            metadata={
                "provider": "linear",
                "issue_id": issue_id,
                "comment_id": comment_id,
                "dag_id": dag.id,
                "queued_nodes": queued_names,
            },
        )
        return task.id

    async def _handle_linear_plan_revision_command(
        self,
        agent_session_id: str,
        issue_id: str,
        comment_id: str | None,
        body: str,
        actor: str,
        command: PlanRevisionCommand,
    ) -> str | None:
        await self._repository.record_session_event(
            session_id=agent_session_id,
            direction="inbound",
            event_type="revise_plan_command",
            actor=actor,
            message=body,
            metadata={"comment_id": comment_id} if comment_id else {},
        )
        task = await self._repository.find_task_by_external_id(command.external_id)
        if task is None:
            reply_body = f"Task {command.external_id} has no plan to revise."
            if self._issue_tracker is not None:
                await self._issue_tracker.reply(
                    IssueTrackerReply(issue_id=issue_id, body=reply_body)
                )
            return None

        current_dag = _latest_task_dag(task)
        if current_dag is None:
            reply_body = f"Task {command.external_id} has no plan to revise."
            if self._issue_tracker is not None:
                await self._issue_tracker.reply(
                    IssueTrackerReply(issue_id=issue_id, body=reply_body)
                )
            return task.id
        if _dag_execution_started(current_dag):
            reply_body = (
                f"Plan revision rejected for {command.external_id}: execution has "
                "already started. Add implementation feedback as a normal issue "
                "comment or use the node change workflow."
            )
            await self._repository.record_session_event(
                session_id=agent_session_id,
                direction="outbound",
                event_type="plan_revision_rejected",
                actor="system",
                message=reply_body,
                metadata={"dag_id": current_dag.id, "comment_id": comment_id},
            )
            if self._issue_tracker is not None:
                await self._issue_tracker.reply(
                    IssueTrackerReply(issue_id=issue_id, body=reply_body)
                )
            return task.id

        restored = _restored_linear_spec_from_task(task)
        if restored is None:
            reply_body = (
                f"Plan revision rejected for {command.external_id}: the hydrated "
                "Linear spec artifact is missing."
            )
            if self._issue_tracker is not None:
                await self._issue_tracker.reply(
                    IssueTrackerReply(issue_id=issue_id, body=reply_body)
                )
            return task.id

        task_event, spec_bundle, hydrated_spec_artifact_id = restored
        registered_repos = await self._repository.list_repos(status="active")
        dag_plan = await self._plan_linear_spec_dag(
            task_id=task.id,
            task_event=task_event,
            spec_bundle=spec_bundle,
            registered_repos=registered_repos,
            revision_feedback=command.feedback,
            previous_subtasks=_subtasks_from_dag(current_dag),
        )
        if dag_plan.planning_failed or not dag_plan.subtasks:
            reply_body = _linear_planning_failed_reply(
                external_id=command.external_id,
                dag_plan=dag_plan,
            )
            await self._repository.record_audit_event(
                action="task.dag_revision_planning_failed",
                actor=actor,
                target_type="task",
                target_id=task.id,
                metadata={
                    "previous_dag_id": current_dag.id,
                    "fallback_reason": dag_plan.fallback_reason,
                    "validation_errors": dag_plan.validation_errors or [],
                    "planner_attempts": dag_plan.planner_attempts,
                    "feedback": command.feedback,
                },
            )
            if self._issue_tracker is not None:
                await self._issue_tracker.reply(
                    IssueTrackerReply(issue_id=issue_id, body=reply_body)
                )
            return task.id

        await self._repository.update_task_dag_status(current_dag.id, "superseded")
        new_dag = await self._repository.create_task_dag(
            task_id=task.id,
            subtasks=dag_plan.subtasks,
        )
        node_metadata = _linear_spec_node_metadata(
            task_event=task_event,
            spec_bundle=spec_bundle,
            dag_plan=dag_plan,
            hydrated_spec_artifact_id=hydrated_spec_artifact_id,
        )
        node_metadata["revision_feedback"] = command.feedback
        node_metadata["supersedes_dag_id"] = current_dag.id
        for node in new_dag.nodes:
            await self._repository.update_dag_node_metadata(
                dag_id=new_dag.id,
                node_key=node.node_key,
                metadata=node_metadata,
            )
        await self._repository.update_task_status(
            task_id=task.id,
            status="needs_plan_approval",
        )
        planned_node_keys = [node.node_key for node in new_dag.nodes]
        reply_body = _linear_assignment_reply(
            external_id=command.external_id,
            repo=task.repo,
            dag_template="linear-spec",
            first_dag_node=None,
            first_dag_node_status=None,
            spec_bundle=spec_bundle,
            dag_plan=dag_plan,
            dag_id=new_dag.id,
            planned_node_keys=planned_node_keys,
            plan_approval_required=True,
            revision_feedback=command.feedback,
        )
        await self._repository.record_session_event(
            session_id=agent_session_id,
            direction="outbound",
            event_type="plan_revised",
            actor="system",
            message=reply_body,
            metadata={
                "comment_id": comment_id,
                "previous_dag_id": current_dag.id,
                "dag_id": new_dag.id,
                "node_keys": planned_node_keys,
            },
        )
        if self._issue_tracker is not None:
            await self._issue_tracker.reply(
                IssueTrackerReply(issue_id=issue_id, body=reply_body)
            )
        await self._repository.record_audit_event(
            action="task.plan_revised",
            actor=actor,
            target_type="task",
            target_id=task.id,
            metadata={
                "provider": "linear",
                "issue_id": issue_id,
                "comment_id": comment_id,
                "previous_dag_id": current_dag.id,
                "dag_id": new_dag.id,
                "node_keys": planned_node_keys,
                "feedback": command.feedback,
                "planning_strategy": dag_plan.strategy,
            },
        )
        return task.id

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
            payload = await self._hydrate_linear_document_context(
                payload=payload,
                task_event=task_event,
                inbound_event_id=result.event.id,
            )
            payload = await self._hydrate_linear_design_context(
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
                "execution_mode": task_event.execution_mode,
            },
        )
        hydrated_spec_artifact_id: str | None = None
        if spec_bundle is not None:
            artifact = await self._repository.create_task_artifact(
                task_id=task.id,
                kind="hydrated_spec",
                name=f"{task_event.external_id}:hydrated-spec",
                content=spec_bundle.to_artifact_content(task_event),
                metadata=spec_bundle.to_metadata(),
            )
            hydrated_spec_artifact_id = artifact.id
            await self._repository.record_audit_event(
                action="task.spec_ingested",
                actor="system",
                target_type="task",
                target_id=task.id,
                metadata={
                    **spec_bundle.to_metadata(),
                    "artifact_id": artifact.id,
                },
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
                metadata={
                    "spec_ingestion": spec_bundle.to_metadata(),
                    "hydrated_spec_artifact_id": hydrated_spec_artifact_id,
                },
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
        task_metadata: dict[str, object] = _task_execution_metadata(
            task_event=task_event,
            default_execution_mode=self._settings.agent_default_execution_mode,
        )
        repo_name: str | None = None
        dag_template: str | None = None
        dag = None
        dag_plan: LinearDagPlan | None = None
        first_dag_node: str | None = None
        first_dag_node_status: str | None = None
        planned_node_keys: list[str] = []
        plan_approval_required = False
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

            task_metadata.update({
                "repo_provider": repo.provider,
                "repo_clone_url": repo.clone_url,
                "repo_default_branch": repo.default_branch,
                "repo_metadata": dict(repo.metadata_json),
            })
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
            task_metadata["spec_ingestion"] = spec_bundle.to_metadata()
            task_metadata["hydrated_spec_artifact_id"] = hydrated_spec_artifact_id
        parent_orchestration_deferred = self._defer_parent_orchestration_for_dag(
            source=source,
            task_event=task_event,
            spec_bundle=spec_bundle,
        )
        if source == "linear" and task_event.issue_id:
            await self._repository.create_agent_session(
                task_id=task.id,
                provider="linear",
                external_thread_id=task_event.issue_id,
                hermes_session_id=None,
                repo=effective_repo,
            )
        if self._task_orchestrator is not None and not parent_orchestration_deferred:
            runtime_sync = await sync_runtime_repositories_for_execution(
                repository=self._repository,
                runtime_repo_registry=self._runtime_repo_registry,
                requested_repo=effective_repo,
            )
            if runtime_sync is not None:
                task_metadata["runtime_repo_sync"] = {
                    "provider": runtime_sync.provider,
                    "workspace_id": runtime_sync.workspace_id,
                    "repo_count": runtime_sync.repo_count,
                    "urls": list(runtime_sync.urls),
                }
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
            await record_llm_cost_ledger(
                repository=self._repository,
                task_id=task.id,
                usage=(external_task.metadata or {}).get("llm_observability"),
                source="task_orchestrator.create_task",
                source_id=external_task.external_task_id,
                metadata={
                    "provider": self._task_orchestrator.provider,
                    "external_id": task_event.external_id,
                },
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
                    "llm_observability": (external_task.metadata or {}).get(
                        "llm_observability"
                    ),
                },
            )
        elif parent_orchestration_deferred:
            await self._repository.record_audit_event(
                action="task.parent_orchestration_deferred",
                actor="system",
                target_type="task",
                target_id=task.id,
                metadata={
                    "provider": (
                        self._task_orchestrator.provider
                        if self._task_orchestrator is not None
                        else None
                    ),
                    "external_id": task_event.external_id,
                    "reason": "dag_node_execution_boundary",
                    "dag_template": task_event.dag_template,
                    "spec_repo_scope": (
                        spec_bundle.repo_scope.scope if spec_bundle is not None else None
                    ),
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
            and not parent_orchestration_deferred
        ):
            try:
                await self._start_linear_agent_session(
                    task_id=task.id,
                    task_event=task_event,
                    repo=effective_repo,
                    spec_bundle=spec_bundle,
                )
            except HermesSessionError as exc:
                await self._record_linear_agent_session_start_failed(
                    task_id=task.id,
                    task_event=task_event,
                    repo=effective_repo,
                    error=str(exc),
                    usage=exc.usage,
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
                    extra_metadata=task_metadata,
                )
        elif (
            source == "linear"
            and spec_bundle is not None
            and self._linear_spec_dag_requested(spec_bundle)
        ):
            dag_template = "linear-spec"
            dag_plan = await self._plan_linear_spec_dag(
                task_id=task.id,
                task_event=task_event,
                spec_bundle=spec_bundle,
                registered_repos=registered_repos,
            )
            if dag_plan.planning_failed or not dag_plan.subtasks:
                task = await self._repository.update_task_status(
                    task_id=task.id,
                    status="planning_failed",
                )
                await self._repository.record_audit_event(
                    action="task.dag_planning_failed",
                    actor="system",
                    target_type="task",
                    target_id=task.id,
                    metadata={
                        "template": dag_template,
                        "strategy": dag_plan.strategy,
                        "fallback_reason": dag_plan.fallback_reason,
                        "validation_error": dag_plan.validation_error,
                        "validation_errors": dag_plan.validation_errors or [],
                        "planner_attempts": dag_plan.planner_attempts,
                        "model_provider": dag_plan.model_provider,
                        "model": dag_plan.model,
                        "failure_message": dag_plan.failure_message,
                    },
                )
                if self._issue_tracker is not None and task_event.issue_id:
                    await self._issue_tracker.reply(
                        IssueTrackerReply(
                            issue_id=task_event.issue_id,
                            body=_linear_planning_failed_reply(
                                external_id=task_event.external_id,
                                dag_plan=dag_plan,
                            ),
                        )
                    )
                return task.id
            dag = await self._repository.create_task_dag(
                task_id=task.id,
                subtasks=dag_plan.subtasks,
            )
            planned_node_keys = [node.node_key for node in dag.nodes]
            node_metadata = _linear_spec_node_metadata(
                task_event=task_event,
                spec_bundle=spec_bundle,
                dag_plan=dag_plan,
                hydrated_spec_artifact_id=hydrated_spec_artifact_id,
            )
            for node in dag.nodes:
                await self._repository.update_dag_node_metadata(
                    dag_id=dag.id,
                    node_key=node.node_key,
                    metadata=node_metadata,
                )
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
                    "validation_error": dag_plan.validation_error,
                    "validation_errors": dag_plan.validation_errors or [],
                    "planner_attempts": dag_plan.planner_attempts,
                    "validation_node_added": dag_plan.validation_node_added,
                    "node_quality_gates_enabled": dag_plan.node_quality_gates_enabled,
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
            plan_approval_required = self._settings.linear_plan_approval_required
            if plan_approval_required:
                task = await self._repository.update_task_status(
                    task_id=task.id,
                    status="needs_plan_approval",
                )
                await self._repository.record_audit_event(
                    action="task.plan_approval_requested",
                    actor="system",
                    target_type="task",
                    target_id=task.id,
                    metadata={
                        "template": dag_template,
                        "dag_id": dag.id,
                        "node_keys": planned_node_keys,
                    },
                )
            elif self._task_orchestrator is not None:
                ready_nodes = [
                    node
                    for node in dag.nodes
                    if node.status == "ready" and not node.depends_on
                ]
                queued_nodes = await self._enqueue_ready_dag_nodes(
                    dag=dag,
                    task=task,
                    ready_nodes=ready_nodes,
                    extra_metadata=node_metadata,
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
                dag_plan=dag_plan,
                dag_id=dag.id if dag is not None else None,
                planned_node_keys=planned_node_keys,
                plan_approval_required=plan_approval_required,
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

    async def _hydrate_linear_document_context(
        self,
        payload: dict[str, object],
        task_event: NormalizedTaskEvent,
        inbound_event_id: str,
    ) -> dict[str, object]:
        if self._document_context is None:
            return payload
        fetched_documents = []
        for url in linear_document_urls(payload, task_event):
            try:
                document = await self._document_context.fetch(url)
            except DocumentContextError as exc:
                await self._repository.record_audit_event(
                    action="linear.document_hydration_failed",
                    actor="system",
                    target_type="inbound_event",
                    target_id=inbound_event_id,
                    metadata={
                        "url": url,
                        "external_id": task_event.external_id,
                        "reason": str(exc),
                    },
                )
                continue
            if document is None or not document.text:
                continue
            fetched_documents.append(document)
        if not fetched_documents:
            return payload

        merged = _merge_linear_document_context(payload, fetched_documents)
        await self._repository.record_audit_event(
            action="linear.documents_hydrated",
            actor="system",
            target_type="inbound_event",
            target_id=inbound_event_id,
            metadata={
                "external_id": task_event.external_id,
                "document_count": len(fetched_documents),
                "providers": [document.provider for document in fetched_documents],
                "urls": [document.url for document in fetched_documents],
            },
        )
        return merged

    async def _hydrate_linear_design_context(
        self,
        payload: dict[str, object],
        task_event: NormalizedTaskEvent,
        inbound_event_id: str,
    ) -> dict[str, object]:
        if self._design_context is None:
            return payload
        fetched_designs = []
        for reference in linear_design_references(payload, task_event):
            try:
                design = await self._design_context.fetch(
                    reference.url,
                    title=reference.title,
                    content_type=reference.content_type,
                )
            except DesignContextError as exc:
                await self._repository.record_audit_event(
                    action="linear.design_hydration_failed",
                    actor="system",
                    target_type="inbound_event",
                    target_id=inbound_event_id,
                    metadata={
                        "url": reference.url,
                        "kind": reference.kind,
                        "external_id": task_event.external_id,
                        "reason": str(exc),
                    },
                )
                continue
            if design is None or not design.summary:
                continue
            fetched_designs.append(design)
        if not fetched_designs:
            return payload

        merged = _merge_linear_design_context(payload, fetched_designs)
        await self._repository.record_audit_event(
            action="linear.designs_hydrated",
            actor="system",
            target_type="inbound_event",
            target_id=inbound_event_id,
            metadata={
                "external_id": task_event.external_id,
                "design_count": len(fetched_designs),
                "providers": [design.provider for design in fetched_designs],
                "urls": [design.url for design in fetched_designs],
            },
        )
        return merged

    async def _plan_linear_spec_dag(
        self,
        task_id: str,
        task_event: NormalizedTaskEvent,
        spec_bundle: SpecIngestionBundle,
        registered_repos: list[object],
        revision_feedback: str | None = None,
        previous_subtasks: list[Subtask] | None = None,
    ) -> LinearDagPlan:
        repo_names = list(spec_bundle.selected_repos)
        repo_contexts: dict[str, object] = {}
        fallback_reason: str | None = None
        model_provider: str | None = None
        model: str | None = None
        validation_error: str | None = None
        validation_errors: list[str] = []
        planned_subtasks: list[Subtask] = []
        planner_attempts = 0

        if self._model_planning_configured():
            repo_contexts = await self._linear_planner_repo_contexts(
                task_event=task_event,
                spec_bundle=spec_bundle,
                repo_names=repo_names,
            )
            base_prompt = _linear_spec_planner_prompt(
                task_event=task_event,
                spec_bundle=spec_bundle,
                repo_contexts=repo_contexts,
                revision_feedback=revision_feedback,
                previous_subtasks=previous_subtasks,
            )
            try:
                for attempt in range(3):
                    prompt = (
                        base_prompt
                        if attempt == 0
                        else _linear_spec_planner_retry_prompt(
                            base_prompt,
                            validation_error or "model plan failed validation",
                            escalation=attempt == 2,
                        )
                    )
                    request_metadata = {
                        "source": "linear",
                        "external_id": task_event.external_id,
                        "attempt": str(attempt + 1),
                    }
                    if attempt == 2:
                        escalation_model = (
                            self._settings.openai_planner_escalation_model
                            if self._settings.model_provider == "openai"
                            else self._settings.claude_default_model or ""
                        )
                        if escalation_model:
                            request_metadata["model"] = escalation_model
                    response = await self._model_provider.complete(
                        ModelRequest(
                            role="plan_agent",
                            prompt=prompt,
                            metadata=request_metadata,
                        )
                    )
                    planner_attempts = attempt + 1
                    model_provider = response.provider
                    model = response.model
                    await record_llm_cost_ledger(
                        repository=self._repository,
                        task_id=task_id,
                        usage=response.usage,
                        source=(
                            "model_provider.plan_agent"
                            if attempt == 0
                            else (
                                "model_provider.plan_agent.escalation"
                                if attempt == 2
                                else "model_provider.plan_agent.retry"
                            )
                        ),
                        source_id=response.request_id,
                        metadata={
                            "provider": response.provider,
                            "model": response.model,
                            "external_id": task_event.external_id,
                            "attempt": attempt + 1,
                        },
                    )
                    parsed_subtasks = DagDecomposer().parse_subtasks(response.content)
                    planned_subtasks, validation_error = _valid_planned_subtasks(
                        subtasks=parsed_subtasks,
                        allowed_repos=set(repo_names),
                    )
                    if validation_error:
                        validation_errors.append(validation_error)
                    planned_subtasks = _prepare_linear_spec_subtasks(
                        planned_subtasks,
                        task_event=task_event,
                        spec_bundle=spec_bundle,
                        revision_feedback=revision_feedback,
                        repo_names=repo_names,
                    )
                    if planned_subtasks:
                        break
                if not planned_subtasks:
                    fallback_reason = "invalid_model_plan"
            except ModelProviderError as exc:
                fallback_reason = "model_provider_error"
                validation_error = str(exc)
                validation_errors.append(str(exc))

        if planned_subtasks:
            return LinearDagPlan(
                subtasks=planned_subtasks,
                strategy="model" if not validation_errors else "model_repaired",
                node_keys=[subtask.id for subtask in planned_subtasks],
                repo_contexts=repo_contexts,
                validation_error=validation_error,
                validation_errors=validation_errors,
                planner_attempts=planner_attempts,
                validation_node_added=False,
                node_quality_gates_enabled=True,
                model_provider=model_provider,
                model=model,
            )

        fallback_reason = fallback_reason or "model_planning_unavailable"
        fallback_subtasks = _prepare_linear_spec_subtasks(
            _deterministic_linear_spec_subtasks(
                task_event=task_event,
                spec_bundle=spec_bundle,
                validation_errors=validation_errors,
                revision_feedback=revision_feedback,
            ),
            task_event=task_event,
            spec_bundle=spec_bundle,
            revision_feedback=revision_feedback,
            repo_names=repo_names,
        )
        if fallback_subtasks:
            return LinearDagPlan(
                subtasks=fallback_subtasks,
                strategy="semantic_fallback",
                node_keys=[subtask.id for subtask in fallback_subtasks],
                repo_contexts=repo_contexts,
                fallback_reason=fallback_reason,
                validation_error=validation_error,
                validation_errors=validation_errors,
                planner_attempts=planner_attempts,
                validation_node_added=False,
                node_quality_gates_enabled=True,
                model_provider=model_provider,
                model=model,
            )

        return LinearDagPlan(
            subtasks=[],
            strategy="planning_failed",
            node_keys=[],
            repo_contexts=repo_contexts,
            fallback_reason=fallback_reason,
            validation_error=validation_error,
            validation_errors=validation_errors,
            planner_attempts=planner_attempts,
            planning_failed=True,
            failure_message="The planner could not produce a DAG and no repository scope exists.",
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

    def _linear_spec_dag_requested(self, spec_bundle: SpecIngestionBundle) -> bool:
        return spec_bundle.repo_scope.scope in {"single_repo", "multi_repo"}

    def _defer_parent_orchestration_for_dag(
        self,
        *,
        source: str,
        task_event: NormalizedTaskEvent,
        spec_bundle: SpecIngestionBundle | None,
    ) -> bool:
        if source != "linear":
            return False
        if task_event.dag_template:
            return True
        return spec_bundle is not None and self._linear_spec_dag_requested(spec_bundle)

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
                contexts[repo_name] = bounded_graph_context(
                    status="unavailable",
                    reason=str(exc),
                )
                continue
            contexts[repo_name] = bounded_graph_context(
                status="available",
                provider=result.provider,
                answer=result.answer,
                references=result.references,
                max_chars=self._settings.graphify_context_max_chars,
                max_references=self._settings.graphify_context_max_references,
            )
        return contexts

    async def _repo_context_for_task(
        self,
        repo: str,
        task_event: NormalizedTaskEvent,
    ) -> dict[str, object] | None:
        if self._graph_store is None:
            return None
        if not self._settings.vendor_http_enabled:
            return bounded_graph_context(
                status="unavailable",
                reason="graph store access is disabled",
            )
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
            return bounded_graph_context(status="unavailable", reason=str(exc))
        return bounded_graph_context(
            status="available",
            provider=result.provider,
            answer=result.answer,
            references=result.references,
            max_chars=self._settings.graphify_context_max_chars,
            max_references=self._settings.graphify_context_max_references,
        )

    async def _enqueue_first_ready_dag_node(
        self,
        dag,
        task,
        extra_metadata: dict[str, object] | None = None,
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
            extra_metadata=extra_metadata,
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
            if node.orchestrator_task_id:
                queued_nodes.append(node)
                continue
            node = await self._repository.update_dag_node_status(
                dag_id=dag.id,
                node_key=node.node_key,
                status="queued",
                orchestrator_status="queued",
            )
            metadata = await build_dag_node_execution_metadata(
                dag=dag,
                task=task,
                node=node,
                repository=self._repository,
                graph_store=self._graph_store,
                settings=self._settings,
            )
            node_metadata = dict(getattr(node, "metadata_json", {}) or {})
            if node_metadata:
                metadata.update(node_metadata)
            if extra_metadata:
                metadata.update(extra_metadata)
            node_execution_mode = _str_value(node_metadata.get("node_execution_mode"))
            if node_execution_mode:
                metadata["execution_mode"] = node_execution_mode
            metadata["execution_policy"] = execution_policy_metadata(
                normalize_execution_mode(
                    metadata.get("execution_mode"),
                    default=self._settings.agent_default_execution_mode,
                )
            )
            metadata["orchestrator_idempotency_key"] = _dag_node_idempotency_key(
                dag_id=dag.id,
                node_key=node.node_key,
                metadata=metadata,
            )
            metadata = sanitize_write_metadata(
                metadata,
                execution_mode=normalize_execution_mode(
                    metadata.get("execution_mode"),
                    default=self._settings.agent_default_execution_mode,
                ),
            )
            try:
                runtime_sync = await sync_runtime_repositories_for_execution(
                    repository=self._repository,
                    runtime_repo_registry=self._runtime_repo_registry,
                    requested_repo=node.repo,
                )
            except RuntimeRepoRegistryError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=str(exc),
                ) from exc
            if runtime_sync is not None:
                metadata["runtime_repo_sync"] = {
                    "provider": runtime_sync.provider,
                    "workspace_id": runtime_sync.workspace_id,
                    "repo_count": runtime_sync.repo_count,
                    "urls": list(runtime_sync.urls),
                }
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
            await record_llm_cost_ledger(
                repository=self._repository,
                task_id=task.id,
                usage=(external_task.metadata or {}).get("llm_observability"),
                source="task_orchestrator.create_dag_node",
                source_id=external_task.external_task_id,
                dag_id=dag.id,
                node_key=node.node_key,
                metadata={
                    "provider": self._task_orchestrator.provider,
                    "external_id": task.external_id,
                },
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
            metadata={
                "message_id": response.message_id,
                "llm_observability": response.usage,
            },
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
                "llm_observability": response.usage,
            },
        )
        await record_llm_cost_ledger(
            repository=self._repository,
            task_id=task_id,
            usage=response.usage,
            source="hermes_session.start",
            source_id=response.session_id,
            metadata={
                "provider": "linear",
                "issue_id": task_event.issue_id,
            },
        )

    async def _record_linear_agent_session_start_failed(
        self,
        task_id: str,
        task_event: NormalizedTaskEvent,
        repo: str | None,
        error: str,
        usage: dict[str, object] | None,
    ) -> None:
        agent_session = await self._repository.create_agent_session(
            task_id=task_id,
            provider="linear",
            external_thread_id=task_event.issue_id,
            hermes_session_id=None,
            repo=repo,
        )
        await self._repository.record_session_event(
            session_id=agent_session.id,
            direction="outbound",
            event_type="session_start_failed",
            actor="system",
            message=error,
            metadata={
                "provider": "hermes",
                "llm_observability": usage,
            },
        )
        await self._repository.record_audit_event(
            action="agent_session.start_failed",
            actor="system",
            target_type="agent_session",
            target_id=agent_session.id,
            metadata={
                "provider": "linear",
                "issue_id": task_event.issue_id,
                "error": error,
                "llm_observability": usage,
            },
        )
        await record_llm_cost_ledger(
            repository=self._repository,
            task_id=task_id,
            usage=usage,
            source="hermes_session.start_failed",
            source_id=agent_session.id,
            metadata={
                "provider": "linear",
                "issue_id": task_event.issue_id,
                "error": error,
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
        quality_gate = None
        if task_update.status in {"merged", "in_review"}:
            quality_gate = evaluate_completion_quality_gate(
                metadata=dict(node.metadata_json) if node.metadata_json else {},
                external_metadata=task_update.metadata or {},
                expected_pr_reference=(
                    f"dag/{task_update.dag_id}/{task_update.dag_node_key}"
                ),
            )
            if quality_gate.satisfied and task_update.status == "merged":
                orchestration_status = "completed"
                node_status = "completed"
            elif quality_gate.satisfied:
                orchestration_status = task_update.status
                node_status = task_update.status
            else:
                orchestration_status = "needs_changes"
                node_status = "needs_changes"

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
                        **(
                            {"quality_gate": quality_gate_metadata(quality_gate)}
                            if quality_gate is not None
                            else {}
                        ),
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
            await self._sync_active_runtime_repositories()
        else:
            await self._repository.update_dag_node_status(
                dag_id=task_update.dag_id,
                node_key=task_update.dag_node_key,
                status=node_status,
                orchestrator_status=orchestration_status,
                metadata={
                    **_pr_node_metadata(task_update),
                    **(
                        {"quality_gate": quality_gate_metadata(quality_gate)}
                        if quality_gate is not None
                        else {}
                    ),
                },
            )
            await self._update_latest_execution_from_pr(task_update, node_status)
            if node_status not in {
                "queued",
                "running",
                "needs_input",
                "needs_changes",
                "pr_open",
                "in_review",
            }:
                await self._sync_active_runtime_repositories()

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

    async def _sync_active_runtime_repositories(self) -> None:
        try:
            await sync_runtime_repositories_for_execution(
                repository=self._repository,
                runtime_repo_registry=self._runtime_repo_registry,
                requested_repo=None,
            )
        except RuntimeRepoRegistryError as exc:
            await self._repository.record_audit_event(
                action="runtime_repo_sync.prune_failed",
                actor="system",
                target_type="runtime_repo_registry",
                target_id=getattr(self._runtime_repo_registry, "provider", "unknown"),
                metadata={"error": str(exc)},
            )

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
            if not _allow_unsigned_webhooks(self._settings):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Webhook signing secret is not configured",
                )
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


def _list_value(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _str_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


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
    dag_plan: LinearDagPlan | None = None,
    dag_id: str | None = None,
    planned_node_keys: list[str] | None = None,
    plan_approval_required: bool = False,
    revision_feedback: str | None = None,
) -> str:
    lines = [
        (
            f"Revised plan for {external_id}."
            if revision_feedback
            else f"Accepted {external_id}."
        ),
        f"Repo: {repo or 'none'}.",
    ]
    if revision_feedback:
        lines.append(f"Revision feedback: {revision_feedback}")
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
    if dag_plan is not None:
        lines.append(f"Planning strategy: {dag_plan.strategy}.")
        if dag_plan.node_quality_gates_enabled:
            lines.append(
                "Validation: each node is quality-gated before downstream nodes run."
            )
        if dag_plan.fallback_reason:
            lines.append(f"Fallback reason: {dag_plan.fallback_reason}.")
        if dag_plan.validation_errors:
            lines.append(
                "Planner validation notes: "
                + "; ".join(dag_plan.validation_errors[:3])
                + ("." if len(dag_plan.validation_errors) <= 3 else "; ...")
            )
        if dag_id:
            lines.append(f"Plan id: {dag_id}.")
        if dag_plan.subtasks:
            lines.append("Plan detail:")
            for index, subtask in enumerate(dag_plan.subtasks, start=1):
                lines.extend(_linear_plan_node_lines(index, subtask))
    if plan_approval_required:
        lines.append(f"Plan approval required: reply /approve-plan {external_id} to start.")
        lines.append(
            f"To change this plan before execution: /revise-plan {external_id} <feedback>."
        )
    if first_dag_node:
        lines.append(f"First DAG node queued: {first_dag_node} ({first_dag_node_status}).")
    else:
        lines.append("First DAG node queued: none.")
    lines.append(f"Commands: /status {external_id}, /context {external_id}, /agents {external_id}.")
    return "\n".join(lines)


def _linear_plan_node_lines(index: int, subtask: Subtask) -> list[str]:
    metadata = subtask.metadata
    lines = [
        f"{index}. `{subtask.id}` - {subtask.title}",
        f"   Repo: {subtask.repo or 'none'}",
        f"   Depends on: {', '.join(subtask.depends_on) if subtask.depends_on else 'none'}",
    ]
    for key, label in (
        ("reasoning", "Why"),
        ("expected_changes", "Expected changes"),
        ("test_scope", "Tests"),
        ("risk_or_dependency", "Risk/dependency"),
    ):
        value = _str_value(metadata.get(key))
        if value:
            lines.append(f"   {label}: {_compact_line(value)}")
    criteria = [
        criterion
        for criterion in subtask.acceptance_criteria[:3]
        if isinstance(criterion, str) and criterion.strip()
    ]
    if criteria:
        lines.append("   Acceptance: " + " | ".join(_compact_line(item) for item in criteria))
    return lines


def _linear_planning_failed_reply(
    *,
    external_id: str,
    dag_plan: LinearDagPlan,
) -> str:
    lines = [
        f"Planning failed for {external_id}; no execution was queued.",
        f"Strategy: {dag_plan.strategy}.",
    ]
    if dag_plan.fallback_reason:
        lines.append(f"Reason: {dag_plan.fallback_reason}.")
    if dag_plan.validation_errors:
        lines.append("Validation errors: " + "; ".join(dag_plan.validation_errors[:5]))
    if dag_plan.failure_message:
        lines.append(dag_plan.failure_message)
    lines.append(f"Revise with: /revise-plan {external_id} <feedback>.")
    return "\n".join(lines)


def _task_execution_metadata(
    *,
    task_event: NormalizedTaskEvent,
    default_execution_mode: str,
) -> dict[str, object]:
    execution_mode = normalize_execution_mode(
        task_event.execution_mode,
        default=default_execution_mode,
    )
    return {
        "execution_mode": execution_mode,
        "execution_policy": execution_policy_metadata(execution_mode),
        "user_intent": _user_intent_metadata(task_event),
    }


def _user_intent_metadata(task_event: NormalizedTaskEvent) -> dict[str, object]:
    return {
        "source": task_event.source,
        "external_id": task_event.external_id,
        "issue_id": task_event.issue_id,
        "title": task_event.title,
        "body": task_event.body,
        "url": task_event.url,
    }


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
    revision_feedback: str | None = None,
    previous_subtasks: list[Subtask] | None = None,
) -> str:
    lines = [
        "Create a reviewable implementation DAG for this Linear ticket.",
        "Return only JSON: an array of objects with id, title, repo, depends_on, "
        "acceptance_criteria, and metadata.",
        "Every id must be snake_case and unique.",
        "Every code node must name exactly one repo from the allowed repos.",
        "For metadata include: reasoning, expected_changes, test_scope, and "
        "risk_or_dependency. The Linear plan comment will show these fields to the human.",
        "Plan for trunk-based development: each executable DAG node should map to "
        "one small PR branched from the repo default branch.",
        "Decompose by reviewable code boundaries when the task needs it: "
        "types/dataclasses/schemas, clients/adapters, services/utilities, then the "
        "final stitching or runtime wiring logic. Do not split just to create more nodes.",
        "Use multiple nodes in the same repo when that makes PRs easier to review, "
        "but each implementation node must be a complete test-first coding loop.",
        "Put final stitching/integration nodes after their foundations and make their "
        "dependencies explicit.",
        "Do not create standalone test nodes for code changes. Merge unit, "
        "regression, contract, and Schemathesis tests into the same implementation "
        "node/PR that changes the production code.",
        "Implementation node titles should make the test scope explicit, for example "
        "`Implement <capability> with unit tests`.",
        "A read-only audit or discovery node is allowed before implementation when "
        "the code area is uncertain; it must not make production changes.",
        "Only use a test-only node when the ticket is explicitly test-hardening "
        "without production code changes.",
        "Use dependencies when one PR should land before another.",
        "Do not create final validation nodes. The platform validates every node "
        "before dependent nodes can run and performs task-level completion checks.",
        f"Ticket: {task_event.external_id} - {task_event.title}",
        f"Allowed repos: {', '.join(spec_bundle.selected_repos)}",
    ]
    if previous_subtasks:
        lines.extend(
            [
                "Current plan to revise:",
                json.dumps(
                    [_subtask_plan_summary(subtask) for subtask in previous_subtasks],
                    sort_keys=True,
                ),
            ]
        )
    if revision_feedback:
        lines.extend(
            [
                "Human revision feedback:",
                revision_feedback,
                "Revise the DAG to account for the feedback before approval.",
            ]
        )
    lines.extend(
        [
            "Spec:",
            _truncated_spec_text(spec_bundle),
            "Design assets:",
            json.dumps(spec_bundle.to_metadata()["design_assets"], sort_keys=True),
            "GraphStore planning context:",
            json.dumps(repo_contexts, sort_keys=True),
        ]
    )
    return "\n".join(lines)


def _linear_spec_planner_retry_prompt(
    original_prompt: str,
    validation_error: str,
    *,
    escalation: bool = False,
) -> str:
    return "\n".join(
        [
            original_prompt,
            "",
            "The previous JSON DAG was rejected by validation.",
            f"Validation error: {validation_error}",
            (
                "This is the escalation attempt. Produce a valid DAG; if the ticket is "
                "ambiguous, include an audit/discovery node instead of collapsing scope."
                if escalation
                else "Repair the DAG using the validation error."
            ),
            "Return a corrected JSON array only.",
            "Do not split tests into standalone nodes for production code changes; "
            "merge those test requirements into the implementation node that owns "
            "the production change.",
            "Do not include a final validation node; platform quality gates validate "
            "each node before downstream execution.",
            "Keep trunk-based PR slicing: foundations first, final stitching or "
            "runtime wiring last when the task has multiple code boundaries.",
            "Every depends_on value must reference another returned node id, and the "
            "graph must be acyclic.",
        ]
    )


def _linear_spec_node_metadata(
    task_event: NormalizedTaskEvent,
    spec_bundle: SpecIngestionBundle,
    dag_plan: LinearDagPlan,
    hydrated_spec_artifact_id: str | None = None,
) -> dict[str, object]:
    return {
        "spec_ingestion": spec_bundle.to_metadata(),
        "hydrated_spec_artifact_id": hydrated_spec_artifact_id,
        "planning_strategy": dag_plan.strategy,
        "planner_attempts": dag_plan.planner_attempts,
        "planner_validation_errors": dag_plan.validation_errors or [],
        "planner_fallback_reason": dag_plan.fallback_reason,
        "validation_node_added": dag_plan.validation_node_added,
        "node_quality_gates_enabled": dag_plan.node_quality_gates_enabled,
        "completion_gate": {
            "mode": "per_node_quality_gates",
            "description": (
                "Each DAG node must satisfy platform validation before dependent "
                "nodes are queued; final task completion is computed by the backend."
            ),
        },
        "user_intent": _user_intent_metadata(task_event),
        "linear_task": {
            "external_id": task_event.external_id,
            "issue_id": task_event.issue_id,
            "title": task_event.title,
            "url": task_event.url,
            "body": task_event.body,
        },
        "execution_contract": {
            "one_code_pr_per_executable_node": True,
            "branch_reference_format": "agent/dag/<external_id>/<dag_id>/<node_key>",
            "pr_reference_format": "dag/<dag_id>/<node_key>",
            "requires_write_execution_mode": True,
        },
    }


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
) -> tuple[list[Subtask], str | None]:
    validation_error = _planned_subtasks_validation_error(subtasks, allowed_repos)
    if validation_error == "split_test_nodes":
        repaired = _merge_test_only_nodes_into_implementations(subtasks)
        repair_error = _planned_subtasks_validation_error(repaired, allowed_repos)
        if repair_error is None:
            return repaired, None
        return [], f"{validation_error}; repair_failed={repair_error}"
    if validation_error is not None:
        return [], validation_error
    return subtasks, None


def _planned_subtasks_validation_error(
    subtasks: list[Subtask],
    allowed_repos: set[str],
) -> str | None:
    if not subtasks:
        return "empty_or_unparseable_plan"
    if any(_is_validation_node(subtask) for subtask in subtasks):
        return "validation_nodes_not_allowed"
    if _has_split_test_nodes(subtasks):
        return "split_test_nodes"
    node_ids = [subtask.id for subtask in subtasks]
    if len(set(node_ids)) != len(node_ids):
        return "duplicate_node_ids"
    if any(not _valid_node_key(subtask.id) for subtask in subtasks):
        invalid = [subtask.id for subtask in subtasks if not _valid_node_key(subtask.id)]
        return f"invalid_node_keys: {', '.join(invalid)}"
    if any(subtask.repo not in allowed_repos for subtask in subtasks):
        invalid_repos = sorted(
            {
                str(subtask.repo)
                for subtask in subtasks
                if subtask.repo not in allowed_repos
            }
        )
        return f"repo_outside_allowed_set: {', '.join(invalid_repos)}"
    node_id_set = set(node_ids)
    for subtask in subtasks:
        if any(dependency not in node_id_set for dependency in subtask.depends_on):
            missing = [
                dependency
                for dependency in subtask.depends_on
                if dependency not in node_id_set
            ]
            return f"missing_dependencies_for_{subtask.id}: {', '.join(missing)}"
        if subtask.id in subtask.depends_on:
            return f"self_dependency: {subtask.id}"
    if _has_dependency_cycle(subtasks):
        return "dependency_cycle"
    if not any(_is_implementation_node(subtask) for subtask in subtasks):
        return "missing_implementation_nodes"
    return None


def _decorate_linear_spec_subtasks(subtasks: list[Subtask]) -> list[Subtask]:
    return [_decorate_linear_spec_subtask(subtask) for subtask in subtasks]


def _prepare_linear_spec_subtasks(
    subtasks: list[Subtask],
    *,
    task_event: NormalizedTaskEvent,
    spec_bundle: SpecIngestionBundle,
    revision_feedback: str | None,
    repo_names: list[str],
) -> list[Subtask]:
    subtasks = _decorate_linear_spec_subtasks(subtasks)
    subtasks = _tag_revision_feedback_subtasks(
        subtasks,
        revision_feedback=revision_feedback,
    )
    return subtasks


def _tag_revision_feedback_subtasks(
    subtasks: list[Subtask],
    *,
    revision_feedback: str | None,
) -> list[Subtask]:
    if not revision_feedback:
        return subtasks

    tagged: list[Subtask] = []
    for subtask in subtasks:
        metadata = dict(subtask.metadata)
        metadata.setdefault("revision_feedback", revision_feedback)
        tagged.append(
            replace(
                subtask,
                metadata=metadata,
            )
        )
    return tagged


def _unique_subtask_id(base: str, subtasks: list[Subtask]) -> str:
    existing = {subtask.id for subtask in subtasks}
    if base not in existing:
        return base
    suffix = 2
    while f"{base}_{suffix}" in existing:
        suffix += 1
    return f"{base}_{suffix}"


def _deterministic_linear_spec_subtasks(
    *,
    task_event: NormalizedTaskEvent,
    spec_bundle: SpecIngestionBundle,
    validation_errors: list[str],
    revision_feedback: str | None,
) -> list[Subtask]:
    repo_names = list(spec_bundle.selected_repos)
    if not repo_names:
        return []
    ticket_key = _node_key(task_event.external_id)
    fallback_metadata = {
        "planner": "deterministic_semantic_fallback",
        "planner_repair": "semantic_fallback_after_invalid_model_plan",
        "planner_validation_errors": list(validation_errors),
    }
    if revision_feedback:
        fallback_metadata["revision_feedback"] = revision_feedback

    if len(repo_names) == 1:
        repo = repo_names[0]
        return [
            Subtask(
                id=f"audit_{ticket_key}_scope",
                title=f"Audit {task_event.external_id} implementation scope",
                repo=repo,
                metadata={
                    **fallback_metadata,
                    "node_execution_kind": "exploration",
                    "reasoning": "Model planning was invalid, so first narrow the exact code path.",
                    "expected_changes": "No production edits; identify files, tests, and risks.",
                    "test_scope": "Identify focused tests to write before implementation.",
                    "risk_or_dependency": "Prevents a broad one-node implementation fallback.",
                },
            ),
            Subtask(
                id=f"implement_{ticket_key}_foundation_with_tests",
                title=f"Implement {task_event.external_id} foundation with tests",
                repo=repo,
                depends_on=(f"audit_{ticket_key}_scope",),
                metadata={
                    **fallback_metadata,
                    "reasoning": (
                        "Creates the foundational types, clients, adapters, helpers, or "
                        "other reusable code needed by the requested change."
                    ),
                    "expected_changes": (
                        "Add the smallest reviewable foundation for the requested behavior."
                    ),
                    "test_scope": (
                        "Write failing focused tests for the new foundation first, then "
                        "production code and green checks."
                    ),
                    "risk_or_dependency": "Depends on the audit node identifying the exact scope.",
                },
            ),
            Subtask(
                id=f"wire_{ticket_key}_behavior_with_tests",
                title=f"Wire {task_event.external_id} behavior with tests",
                repo=repo,
                depends_on=(f"implement_{ticket_key}_foundation_with_tests",),
                metadata={
                    **fallback_metadata,
                    "reasoning": (
                        "Connects the foundation into the runtime flow after the shared "
                        "pieces are available."
                    ),
                    "expected_changes": (
                        "Apply final stitching or runtime wiring for the requested behavior."
                    ),
                    "test_scope": (
                        "Regression or integration-focused tests that prove the wired "
                        "behavior works end to end within the repo boundary."
                    ),
                    "risk_or_dependency": (
                        "Runs after the foundation PR so trunk-based changes stay small "
                        "and ordered."
                    ),
                },
            ),
        ]

    audit_id = f"audit_{ticket_key}_cross_repo_scope"
    subtasks = [
        Subtask(
            id=audit_id,
            title=f"Audit {task_event.external_id} cross-repo scope",
            repo=repo_names[0],
            metadata={
                **fallback_metadata,
                "node_execution_kind": "exploration",
                "reasoning": "Model planning was invalid for a multi-repo ticket.",
                "expected_changes": "No production edits; define repo boundaries and merge order.",
                "test_scope": "Identify per-repo tests and contract checks.",
                "risk_or_dependency": (
                    "Prevents cross-repo PRs from being generated in the wrong order."
                ),
            },
        )
    ]
    implementation_node_ids: list[str] = []
    for repo in repo_names:
        repo_key = _node_key(repo)
        node_id = f"implement_{repo_key}_with_tests"
        implementation_node_ids.append(node_id)
        subtasks.append(
            Subtask(
                id=node_id,
                title=f"Implement {task_event.external_id} changes in {repo} with tests",
                repo=repo,
                depends_on=(audit_id,),
                metadata={
                    **fallback_metadata,
                    "reasoning": f"Owns the test-first implementation loop for {repo}.",
                    "expected_changes": (
                        f"Implement the repo-local portion of {task_event.external_id}."
                    ),
                    "test_scope": (
                        "Focused unit/regression and contract tests when interfaces change."
                    ),
                    "risk_or_dependency": (
                        "Depends on cross-repo scope and must preserve DAG merge order."
                    ),
                },
            )
        )
    stitch_repo = repo_names[0]
    subtasks.append(
        Subtask(
            id=f"stitch_{ticket_key}_cross_repo_flow_with_tests",
            title=f"Stitch {task_event.external_id} cross-repo flow with tests",
            repo=stitch_repo,
            depends_on=tuple(implementation_node_ids),
            metadata={
                **fallback_metadata,
                "reasoning": (
                    "Performs final integration after repo-local PRs are available."
                ),
                "expected_changes": (
                    "Wire the cross-repo behavior or contracts in the owning integration repo."
                ),
                "test_scope": (
                    "Focused integration or contract tests proving the cross-repo flow is "
                    "consistent."
                ),
                "risk_or_dependency": (
                    "Depends on all repo-local implementation nodes and should be the last "
                    "trunk-based PR in the fallback DAG."
                ),
            },
        )
    )
    return subtasks


def _merge_test_only_nodes_into_implementations(subtasks: list[Subtask]) -> list[Subtask]:
    targets_by_test_id: dict[str, str] = {}
    merged_by_id = {subtask.id: subtask for subtask in subtasks}

    for subtask in subtasks:
        if not _is_test_only_node(subtask):
            continue
        target = _test_node_merge_target(subtask, subtasks)
        if target is None:
            return subtasks
        targets_by_test_id[subtask.id] = target.id
        merged_by_id[target.id] = _merge_test_node_into_target(
            target=merged_by_id[target.id],
            test_node=subtask,
        )

    if not targets_by_test_id:
        return subtasks

    repaired: list[Subtask] = []
    for subtask in subtasks:
        if subtask.id in targets_by_test_id:
            continue
        current = merged_by_id[subtask.id]
        rewritten_depends_on = _rewrite_merged_dependencies(
            node_id=current.id,
            depends_on=current.depends_on,
            replacements=targets_by_test_id,
        )
        repaired.append(replace(current, depends_on=tuple(rewritten_depends_on)))
    return repaired


def _test_node_merge_target(test_node: Subtask, subtasks: list[Subtask]) -> Subtask | None:
    implementation_nodes = [
        subtask
        for subtask in subtasks
        if not _is_test_only_node(subtask) and not _is_exploration_node(subtask)
    ]
    same_repo = [
        subtask for subtask in implementation_nodes if subtask.repo == test_node.repo
    ]
    candidates = same_repo if test_node.repo else implementation_nodes
    if not candidates:
        return None

    by_id = {subtask.id: subtask for subtask in candidates}
    for dependency in reversed(test_node.depends_on):
        if dependency in by_id:
            return by_id[dependency]
    return candidates[0]


def _merge_test_node_into_target(*, target: Subtask, test_node: Subtask) -> Subtask:
    acceptance_criteria = list(target.acceptance_criteria)
    _append_unique(
        acceptance_criteria,
        f"Include test scope from merged planner node `{test_node.id}`: {test_node.title}.",
    )
    for criterion in test_node.acceptance_criteria:
        _append_unique(acceptance_criteria, criterion)

    metadata = dict(target.metadata)
    existing_merged = metadata.get("merged_test_nodes", [])
    merged_test_nodes = [
        item
        for item in (existing_merged if isinstance(existing_merged, list) else [])
        if isinstance(item, str)
    ]
    _append_unique(merged_test_nodes, test_node.id)
    metadata["merged_test_nodes"] = merged_test_nodes
    metadata["planner_repair"] = "merged_test_nodes_into_implementation"

    depends_on = _dedupe(
        [
            *target.depends_on,
            *[
                dependency
                for dependency in test_node.depends_on
                if dependency != target.id
            ],
        ]
    )
    title = target.title
    if "test" not in title.lower():
        title = f"{title} with tests"
    return replace(
        target,
        title=title,
        depends_on=tuple(depends_on),
        acceptance_criteria=tuple(acceptance_criteria),
        metadata=metadata,
    )


def _rewrite_merged_dependencies(
    *,
    node_id: str,
    depends_on: tuple[str, ...],
    replacements: dict[str, str],
) -> list[str]:
    rewritten: list[str] = []
    for dependency in depends_on:
        replacement = replacements.get(dependency, dependency)
        if replacement == node_id:
            continue
        _append_unique(rewritten, replacement)
    return rewritten


def _dedupe(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        _append_unique(deduped, value)
    return deduped


def _decorate_linear_spec_subtask(subtask: Subtask) -> Subtask:
    metadata = dict(subtask.metadata)
    acceptance_criteria = list(subtask.acceptance_criteria)
    if _is_validation_node(subtask):
        metadata.setdefault("node_execution_kind", "final_validation")
        metadata.setdefault("node_execution_mode", PLANNING_ONLY)
        metadata.setdefault("tdd_required", False)
        metadata.setdefault("same_pr_tests_required", False)
        return replace(
            subtask,
            acceptance_criteria=tuple(acceptance_criteria),
            metadata=metadata,
        )

    if _is_exploration_node(subtask):
        metadata.setdefault("node_execution_kind", "exploration")
        metadata.setdefault("node_execution_mode", PLANNING_ONLY)
        metadata.setdefault("tdd_required", False)
        _append_unique(
            acceptance_criteria,
            "Audit and document the relevant code paths without production code changes.",
        )
        return replace(
            subtask,
            acceptance_criteria=tuple(acceptance_criteria),
            metadata=metadata,
        )

    metadata.setdefault("node_execution_kind", "implementation")
    metadata.setdefault("tdd_required", True)
    metadata.setdefault("same_pr_tests_required", True)
    metadata.setdefault("contract_tests_required_for_api_changes", True)
    _append_unique(
        acceptance_criteria,
        (
            "Create or update focused tests first and capture failing-test evidence "
            "before production edits."
        ),
    )
    _append_unique(
        acceptance_criteria,
        (
            "Implement production code and keep the implementation plus relevant "
            "unit/regression tests in the same PR."
        ),
    )
    _append_unique(
        acceptance_criteria,
        (
            "If endpoint, schema, webhook, or public contract behavior changes, "
            "add or update contract/Schemathesis tests in the same PR."
        ),
    )
    _append_unique(
        acceptance_criteria,
        (
            "Persist final evidence for failing red-step tests and passing focused, "
            "unit, contract-when-relevant, and configured smoke checks."
        ),
    )
    return replace(
        subtask,
        acceptance_criteria=tuple(acceptance_criteria),
        metadata=metadata,
    )


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _has_split_test_nodes(subtasks: list[Subtask]) -> bool:
    if len(subtasks) < 2:
        return False
    has_implementation = any(
        _is_implementation_node(subtask)
        for subtask in subtasks
    )
    if not has_implementation:
        return False
    return any(
        _is_test_only_node(subtask)
        and _str_value(subtask.metadata.get("node_execution_kind")) != "test_hardening"
        for subtask in subtasks
    )


def _is_test_only_node(subtask: Subtask) -> bool:
    if _is_validation_node(subtask):
        return False
    if subtask.id.endswith("_with_tests"):
        return False
    text = f"{subtask.id} {subtask.title}".lower()
    if (
        (subtask.id.endswith("_tests") and not subtask.id.endswith("_with_tests"))
        or subtask.id.endswith("_regression_tests")
        or text.startswith("add tests")
        or text.startswith("add unit tests")
        or text.startswith("add regression tests")
        or " add tests " in f" {text} "
        or " add unit tests " in f" {text} "
        or " add regression tests " in f" {text} "
    ):
        return True
    has_test_signal = any(
        token in text
        for token in (
            "test",
            "tests",
            "regression",
            "schemathesis",
            "contract_test",
            "contract tests",
        )
    )
    has_implementation_signal = any(
        token in text
        for token in (
            "implement",
            "add ",
            "add sync",
            "wire",
            "refactor",
            "remove",
            "cleanup",
            "support",
            "build",
            "expose",
        )
    )
    return has_test_signal and not has_implementation_signal


def _is_implementation_node(subtask: Subtask) -> bool:
    return (
        not _is_test_only_node(subtask)
        and not _is_validation_node(subtask)
        and not _is_exploration_node(subtask)
    )


def _is_exploration_node(subtask: Subtask) -> bool:
    if _is_validation_node(subtask):
        return False
    kind = _str_value(subtask.metadata.get("node_execution_kind"))
    if kind in {"exploration", "audit", "planning"}:
        return True
    text = f"{subtask.id} {subtask.title}".lower()
    if any(token in text for token in ("implement", "refactor", "wire", "remove", "add ")):
        return False
    return any(
        token in text
        for token in (
            "audit",
            "analyze",
            "investigate",
            "inspect",
            "discover",
            "map ",
            "scope ",
        )
    )


def _is_validation_node(subtask: Subtask) -> bool:
    kind = _str_value(subtask.metadata.get("node_execution_kind"))
    return (
        kind == "final_validation"
        or subtask.metadata.get("validation_node") is True
        or subtask.id.startswith("validate_")
    )


def _subtask_plan_summary(subtask: Subtask) -> dict[str, object]:
    return {
        "id": subtask.id,
        "title": subtask.title,
        "repo": subtask.repo,
        "depends_on": list(subtask.depends_on),
        "acceptance_criteria": list(subtask.acceptance_criteria),
        "metadata": dict(subtask.metadata),
    }


def _subtasks_from_dag(dag) -> list[Subtask]:
    subtasks: list[Subtask] = []
    for node in getattr(dag, "nodes", []) or []:
        metadata = dict(getattr(node, "metadata_json", {}) or {})
        criteria_value = metadata.get("acceptance_criteria")
        criteria = (
            tuple(item for item in criteria_value if isinstance(item, str))
            if isinstance(criteria_value, list)
            else ()
        )
        subtasks.append(
            Subtask(
                id=node.node_key,
                title=node.title,
                repo=node.repo,
                depends_on=tuple(getattr(node, "depends_on", ()) or ()),
                acceptance_criteria=criteria,
                metadata=metadata,
            )
        )
    return subtasks


def _latest_task_dag(task) -> object | None:
    dags = list(getattr(task, "dags", []) or [])
    active_dags = [dag for dag in dags if getattr(dag, "status", None) != "superseded"]
    if not active_dags:
        return None
    return sorted(
        active_dags,
        key=lambda dag: (getattr(dag, "created_at", None), getattr(dag, "id", "")),
        reverse=True,
    )[0]


def _dag_execution_started(dag) -> bool:
    for node in getattr(dag, "nodes", []) or []:
        if getattr(node, "orchestrator_task_id", None):
            return True
        if getattr(node, "status", None) not in {"ready", "blocked"}:
            return True
    return False


def _restored_linear_spec_from_task(
    task,
) -> tuple[NormalizedTaskEvent, SpecIngestionBundle, str | None] | None:
    artifacts = [
        artifact
        for artifact in getattr(task, "artifacts", []) or []
        if getattr(artifact, "kind", None) == "hydrated_spec"
    ]
    if not artifacts:
        return None
    artifact = sorted(
        artifacts,
        key=lambda item: (getattr(item, "created_at", None), getattr(item, "id", "")),
        reverse=True,
    )[0]
    content = _dict_value(getattr(artifact, "content_json", None))
    task_data = _dict_value(content.get("task"))
    repo_scope_data = _dict_value(content.get("repo_scope"))
    repo_matches = []
    for item in _list_value(repo_scope_data.get("repos")):
        row = _dict_value(item)
        repo = _str_value(row.get("repo"))
        if repo:
            repo_matches.append(
                RepoMatch(repo=repo, reason=_str_value(row.get("reason")) or "artifact")
            )
    text_sources = []
    for item in _list_value(content.get("text_sources")):
        row = _dict_value(item)
        text = _str_value(row.get("text"))
        if text is None:
            continue
        text_sources.append(
            TextSource(
                kind=_str_value(row.get("kind")) or "linear",
                title=_str_value(row.get("title")) or "Linear spec",
                text=text,
            )
        )
    design_assets = []
    for item in _list_value(content.get("design_assets")):
        row = _dict_value(item)
        design_assets.append(
            DesignAsset(
                kind=_str_value(row.get("kind")) or "asset",
                title=_str_value(row.get("title")) or "Design asset",
                url=_str_value(row.get("url")),
                content_type=_str_value(row.get("content_type")),
            )
        )
    if not text_sources:
        return None
    task_event = NormalizedTaskEvent(
        source=_str_value(task_data.get("source")) or getattr(task, "source", "linear"),
        external_id=_str_value(task_data.get("external_id")) or task.external_id,
        title=_str_value(task_data.get("title")) or task.title,
        issue_id=_str_value(task_data.get("issue_id")),
        repo=getattr(task, "repo", None),
        url=_str_value(task_data.get("url")),
        body=_str_value(task_data.get("body")),
    )
    spec_bundle = SpecIngestionBundle(
        source=_str_value(content.get("source")) or "linear",
        text_sources=tuple(text_sources),
        design_assets=tuple(design_assets),
        repo_scope=RepoScope(
            scope=_str_value(repo_scope_data.get("scope")) or "unspecified",
            repos=tuple(repo_matches),
            unknown_repos=tuple(
                value
                for value in _list_value(repo_scope_data.get("unknown_repos"))
                if isinstance(value, str)
            ),
        ),
    )
    return task_event, spec_bundle, getattr(artifact, "id", None)


def _linear_spec_bundle_with_resolved_repo(
    spec_bundle: SpecIngestionBundle,
    repo_name: str,
) -> SpecIngestionBundle:
    return SpecIngestionBundle(
        source=spec_bundle.source,
        text_sources=spec_bundle.text_sources,
        design_assets=spec_bundle.design_assets,
        repo_scope=RepoScope(
            scope="single_repo",
            repos=(RepoMatch(repo=repo_name, reason="repo clarification"),),
            unknown_repos=(),
        ),
    )


def _node_key(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", value.lower()).strip("_")
    normalized = re.sub(r"_+", "_", normalized)
    if not normalized:
        normalized = "node"
    if not normalized[0].isalpha():
        normalized = f"node_{normalized}"
    return normalized[:64]


def _compact_line(value: str, limit: int = 180) -> str:
    compacted = " ".join(value.split())
    if len(compacted) <= limit:
        return compacted
    return f"{compacted[: limit - 13].rstrip()}...[truncated]"


def _valid_node_key(value: str) -> bool:
    return re.fullmatch(r"[a-z][a-z0-9_]{1,63}", value) is not None


def _dag_node_idempotency_key(
    *,
    dag_id: str,
    node_key: str,
    metadata: dict[str, object],
) -> str:
    retry_count = metadata.get("retry_count")
    attempt = retry_count if isinstance(retry_count, int) else 0
    return f"{dag_id}:{node_key}:{attempt}"


def _has_dependency_cycle(subtasks: list[Subtask]) -> bool:
    dependencies = {subtask.id: set(subtask.depends_on) for subtask in subtasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visited:
            return False
        if node_id in visiting:
            return True
        visiting.add(node_id)
        for dependency in dependencies[node_id]:
            if visit(dependency):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(visit(node_id) for node_id in dependencies)


def _allow_unsigned_webhooks(settings: Settings) -> bool:
    return bool(settings.allow_unsigned_webhooks) or settings.environment in {
        "local",
        "dev",
        "development",
        "test",
    }


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


def _merge_linear_document_context(
    payload: dict[str, object],
    documents: list[object],
) -> dict[str, object]:
    merged = dict(payload)
    data = dict(_dict_value(merged.get("data")))
    existing_documents = _extract_payload_nodes(data.get("documents"))
    data["documents"] = {
        "nodes": [
            *existing_documents,
            *[_linear_document_payload(document) for document in documents],
        ]
    }
    merged["data"] = data
    return merged


def _merge_linear_design_context(
    payload: dict[str, object],
    designs: list[object],
) -> dict[str, object]:
    merged = dict(payload)
    data = dict(_dict_value(merged.get("data")))
    existing_documents = _extract_payload_nodes(data.get("documents"))
    data["documents"] = {
        "nodes": [
            *existing_documents,
            *[_linear_design_context_payload(design) for design in designs],
        ]
    }
    merged["data"] = data
    return merged


def _linear_document_payload(document) -> dict[str, object]:
    return {
        "id": document.url,
        "title": document.title or document.url,
        "url": document.url,
        "contentType": "text/markdown",
        "content": document.text,
        "provider": document.provider,
        "metadata": document.metadata or {},
    }


def _linear_design_context_payload(design) -> dict[str, object]:
    return {
        "id": design.url,
        "title": design.title or design.url,
        "contentType": "text/markdown",
        "content": design.summary,
        "provider": design.provider,
        "metadata": {
            "hydrated_design_context": True,
            "source_url": design.url,
            **(design.metadata or {}),
        },
    }


def _extract_payload_nodes(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        nodes = value.get("nodes")
        if isinstance(nodes, list):
            return [node for node in nodes if isinstance(node, dict)]
        return [value]
    if isinstance(value, list):
        return [node for node in value if isinstance(node, dict)]
    return []


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
