from __future__ import annotations

from dataclasses import dataclass

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.glue.adversarial_loop import run_adversarial_review_loop
from agentic_sdlc_platform.glue.dag_completion_verifier import (
    CompletionVerification,
    verify_node_completion,
)
from agentic_sdlc_platform.glue.dag_execution import (
    build_dag_node_execution_metadata,
    create_or_start_execution,
)
from agentic_sdlc_platform.glue.execution_policy import (
    WRITE_PR,
    execution_policy_metadata,
    normalize_execution_mode,
    sanitize_write_metadata,
)
from agentic_sdlc_platform.glue.llm_observability import (
    LLM_COST_LEDGER_ARTIFACT_KIND,
    record_llm_cost_ledger,
)
from agentic_sdlc_platform.glue.quality_gate import (
    QualityGateResult,
    evaluate_completion_quality_gate,
    quality_gate_metadata,
)
from agentic_sdlc_platform.glue.runtime_repo_sync import (
    sync_runtime_repositories_for_execution,
)
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.agent_executor import AgentExecutorPort
from agentic_sdlc_platform.ports.graph_store import GraphStorePort
from agentic_sdlc_platform.ports.issue_tracker import IssueTrackerPort, IssueTrackerReply
from agentic_sdlc_platform.ports.model_provider import ModelProviderPort
from agentic_sdlc_platform.ports.runtime_repo_registry import (
    RuntimeRepoRegistryError,
    RuntimeRepoRegistryPort,
)
from agentic_sdlc_platform.ports.task_orchestrator import (
    TaskOrchestratorPort,
    TaskReadRequest,
    TaskRequest,
)


@dataclass(frozen=True)
class DagNodeSyncResult:
    dag_id: str
    node_key: str
    status: str
    orchestrator_status: str | None
    queued_nodes: tuple[str, ...] = ()


class DagNodeOrchestratorSyncService:
    def __init__(
        self,
        *,
        repository: PersistenceRepository,
        task_orchestrator: TaskOrchestratorPort | None,
        graph_store: GraphStorePort | None = None,
        model_provider: ModelProviderPort | None = None,
        settings: Settings | None = None,
        agent_executor: AgentExecutorPort | None = None,
        runtime_repo_registry: RuntimeRepoRegistryPort | None = None,
        issue_tracker: IssueTrackerPort | None = None,
    ) -> None:
        self._repository = repository
        self._task_orchestrator = task_orchestrator
        self._graph_store = graph_store
        self._model_provider = model_provider
        self._settings = settings or Settings()
        self._agent_executor = agent_executor
        self._runtime_repo_registry = runtime_repo_registry
        self._issue_tracker = issue_tracker

    async def sync_active_nodes(self, limit: int = 50) -> list[DagNodeSyncResult]:
        if self._task_orchestrator is None:
            return []
        nodes = await self._repository.list_orchestrated_dag_nodes(limit=limit)
        results: list[DagNodeSyncResult] = []
        for node in nodes:
            try:
                results.append(await self.sync_node(dag_id=node.dag_id, node_key=node.node_key))
            except Exception as exc:  # pragma: no cover - defensive background isolation
                await self._repository.record_audit_event(
                    action="task.dag_node_sync_failed",
                    actor="system",
                    target_type="dag_node",
                    target_id=f"{node.dag_id}:{node.node_key}",
                    metadata={"error": str(exc)},
                )
        return results

    async def sync_node(self, *, dag_id: str, node_key: str) -> DagNodeSyncResult:
        if self._task_orchestrator is None:
            raise ValueError("task orchestrator is not configured")
        dag = await self._repository.get_task_dag(dag_id)
        if dag is None:
            raise LookupError(f"DAG {dag_id} not found")
        node = _node_from_dag(dag, node_key)
        if not node.orchestrator_task_id:
            raise LookupError("DAG node has no orchestrator task")

        external_task = await self._task_orchestrator.read_task(
            TaskReadRequest(
                external_task_id=node.orchestrator_task_id,
                metadata=dict(node.metadata_json),
            )
        )
        status_from_orchestrator = node_status_from_orchestrator(external_task.status)
        external_metadata = external_task.metadata or {}
        metadata = external_metadata
        verification: CompletionVerification | None = None
        quality_gate: QualityGateResult | None = None

        if status_from_orchestrator in {"completed", "in_review"}:
            node_metadata = dict(node.metadata_json)
            verification = verify_node_completion(
                node_key=node.node_key,
                node_title=node.title,
                repo=node.repo,
                metadata=node_metadata,
                external_metadata=external_metadata,
            )
            if status_from_orchestrator == "completed" and verification.satisfied:
                adversarial_loop = await run_adversarial_review_loop(
                    task=dag.task,
                    dag=dag,
                    node=node,
                    repository=self._repository,
                    model_provider=self._model_provider,
                    task_orchestrator=self._task_orchestrator,
                    settings=self._settings,
                    external_metadata=external_metadata,
                )
                metadata = {
                    **external_metadata,
                    **adversarial_loop.node_metadata,
                }
                if adversarial_loop.should_requeue:
                    await self._record_synced_task_usage(
                        task_id=dag.task_id,
                        dag_id=dag_id,
                        node_key=node_key,
                        external_task=external_task,
                    )
                    queued = await self._retry_and_enqueue(
                        dag_id=dag_id,
                        node_key=node_key,
                    )
                    return DagNodeSyncResult(
                        dag_id=dag_id,
                        node_key=node_key,
                        status=queued.status,
                        orchestrator_status=queued.orchestrator_status,
                        queued_nodes=(node_key,),
                    )
                if adversarial_loop.human_intervention_required:
                    synced_node = await self._repository.update_dag_node_status(
                        dag_id=dag_id,
                        node_key=node_key,
                        status="needs_input",
                        orchestrator_status="needs_input",
                        metadata=metadata,
                    )
                    await self._record_synced_task_usage(
                        task_id=dag.task_id,
                        dag_id=dag_id,
                        node_key=node_key,
                        external_task=external_task,
                    )
                    return DagNodeSyncResult(
                        dag_id=dag_id,
                        node_key=node_key,
                        status=synced_node.status,
                        orchestrator_status=synced_node.orchestrator_status,
                    )
                node_metadata = {
                    **node_metadata,
                    **adversarial_loop.node_metadata,
                }
            quality_gate = evaluate_completion_quality_gate(
                metadata=node_metadata,
                external_metadata=metadata,
                expected_pr_reference=f"dag/{dag_id}/{node_key}",
            )
            metadata = {
                **metadata,
                "completion_verification": {
                    "status": verification.status,
                    "missing": list(verification.missing),
                    "follow_up_nodes": [followup.id for followup in verification.followups],
                    "evidence": verification.evidence,
                },
                "quality_gate": quality_gate_metadata(quality_gate),
            }
            if self._should_retry_node(
                node_metadata=node_metadata,
                external_metadata=metadata,
                verification=verification,
                quality_gate=quality_gate,
            ):
                await self._repository.update_dag_node_metadata(
                    dag_id=dag_id,
                    node_key=node_key,
                    metadata={
                        **metadata,
                        "latest_quality_feedback": _quality_feedback(
                            verification=verification,
                            quality_gate=quality_gate,
                        ),
                        "previous_completion_verification": metadata["completion_verification"],
                        "previous_quality_gate": metadata["quality_gate"],
                    },
                )
                await self._record_synced_task_usage(
                    task_id=dag.task_id,
                    dag_id=dag_id,
                    node_key=node_key,
                    external_task=external_task,
                )
                queued = await self._retry_and_enqueue(dag_id=dag_id, node_key=node_key)
                return DagNodeSyncResult(
                    dag_id=dag_id,
                    node_key=node_key,
                    status=queued.status,
                    orchestrator_status=queued.orchestrator_status,
                    queued_nodes=(node_key,),
                )

        user_state = _user_state_metadata(
            node=node,
            status=_synced_node_status(
                status_from_orchestrator=status_from_orchestrator,
                verification=verification,
                quality_gate=quality_gate,
                external_metadata=metadata,
            ),
            status_from_orchestrator=status_from_orchestrator,
            verification=verification,
            quality_gate=quality_gate,
            external_metadata=metadata,
        )
        metadata = {**metadata, **user_state}
        synced_node = await self._repository.update_dag_node_status(
            dag_id=dag_id,
            node_key=node_key,
            status=_synced_node_status(
                status_from_orchestrator=status_from_orchestrator,
                verification=verification,
                quality_gate=quality_gate,
                external_metadata=metadata,
            ),
            orchestrator_status=external_task.status,
            metadata=metadata,
        )
        await self._notify_node_transition(
            task=dag.task,
            node=synced_node,
            previous_node=node,
            metadata=metadata,
        )
        await self._record_synced_task_usage(
            task_id=dag.task_id,
            dag_id=dag_id,
            node_key=node_key,
            external_task=external_task,
        )

        queued_nodes: tuple[str, ...] = ()
        if synced_node.status in {"completed", "failed", "skipped"}:
            await self._repository.refresh_dag_completion_status(dag_id)
        if synced_node.status in {"completed", "skipped"}:
            refreshed = await self._repository.get_task_dag(dag_id)
            if refreshed is not None and refreshed.status != "completed":
                ready_nodes = await self._repository.list_ready_dag_nodes_for_dag(dag_id)
                queued = await self.enqueue_ready_nodes(
                    dag=refreshed,
                    task=refreshed.task,
                    ready_nodes=ready_nodes,
                )
                queued_nodes = tuple(item.node_key for item in queued)
            if not queued_nodes:
                await self._sync_runtime_repositories(requested_repo=None)

        return DagNodeSyncResult(
            dag_id=dag_id,
            node_key=node_key,
            status=synced_node.status,
            orchestrator_status=synced_node.orchestrator_status,
            queued_nodes=queued_nodes,
        )

    async def enqueue_ready_nodes(self, *, dag, task, ready_nodes) -> list[object]:
        if self._task_orchestrator is None:
            return []
        queued_nodes = []
        for node in ready_nodes:
            if node.orchestrator_task_id:
                queued_nodes.append(node)
                continue
            metadata = await build_dag_node_execution_metadata(
                dag=dag,
                task=task,
                node=node,
                repository=self._repository,
                graph_store=self._graph_store,
                settings=self._settings,
            )
            execution_mode = normalize_execution_mode(
                metadata.get("execution_mode"),
                default=self._settings.agent_default_execution_mode,
            )
            metadata["execution_mode"] = execution_mode
            metadata["execution_policy"] = execution_policy_metadata(execution_mode)
            metadata["orchestrator_idempotency_key"] = _dag_node_idempotency_key(
                dag_id=dag.id,
                node_key=node.node_key,
                metadata=metadata,
            )
            task_metadata = sanitize_write_metadata(
                metadata,
                execution_mode=execution_mode,
            )
            try:
                runtime_sync = await self._sync_runtime_repositories(requested_repo=node.repo)
            except RuntimeRepoRegistryError as exc:
                raise RuntimeError(str(exc)) from exc
            if runtime_sync is not None:
                task_metadata["runtime_repo_sync"] = {
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
                    metadata=task_metadata,
                )
            )
            persisted_metadata = {
                **task_metadata,
                **(external_task.metadata or {}),
            }
            queued_node = await self._repository.mark_dag_node_orchestrated(
                dag_id=dag.id,
                node_key=node.node_key,
                orchestrator_task_id=external_task.external_task_id,
                orchestrator_status=external_task.status,
                metadata=persisted_metadata,
            )
            user_state = _user_state_metadata(
                node=queued_node,
                status=queued_node.status,
                status_from_orchestrator=external_task.status,
                verification=None,
                quality_gate=None,
                external_metadata=persisted_metadata,
            )
            persisted_metadata = {**persisted_metadata, **user_state}
            queued_node = await self._repository.update_dag_node_metadata(
                dag_id=dag.id,
                node_key=node.node_key,
                metadata=user_state,
            )
            await create_or_start_execution(
                repository=self._repository,
                agent_executor=self._agent_executor,
                dag=dag,
                task=task,
                node=node,
                metadata=persisted_metadata,
            )
            await self._notify_node_transition(
                task=task,
                node=queued_node,
                previous_node=node,
                metadata=persisted_metadata,
            )
            queued_nodes.append(queued_node)
        return queued_nodes

    async def _notify_node_transition(
        self,
        *,
        task,
        node,
        previous_node,
        metadata: dict[str, object],
    ) -> None:
        if self._issue_tracker is None:
            return
        notification = _node_status_notification(
            task_external_id=getattr(task, "external_id", None),
            node_key=node.node_key,
            status=node.status,
            metadata=metadata,
        )
        if notification is None:
            return
        previous_metadata = dict(getattr(previous_node, "metadata_json", {}) or {})
        if previous_metadata.get("last_status_notification_key") == notification["key"]:
            return
        issue_id = _linear_issue_id(metadata) or _linear_issue_id(previous_metadata)
        if not issue_id:
            return
        try:
            await self._issue_tracker.reply(
                IssueTrackerReply(issue_id=issue_id, body=str(notification["body"]))
            )
        except Exception as exc:  # pragma: no cover - notification must not block DAG sync
            await self._repository.record_audit_event(
                action="task.dag_node_status_notification_failed",
                actor="system",
                target_type="dag_node",
                target_id=f"{node.dag_id}:{node.node_key}",
                metadata={
                    "error": str(exc),
                    "node_key": node.node_key,
                    "status": node.status,
                },
            )
            return
        await self._repository.update_dag_node_metadata(
            dag_id=node.dag_id,
            node_key=node.node_key,
            metadata={"last_status_notification_key": notification["key"]},
        )

    async def _sync_runtime_repositories(
        self,
        *,
        requested_repo: str | None,
    ):
        return await sync_runtime_repositories_for_execution(
            repository=self._repository,
            runtime_repo_registry=self._runtime_repo_registry,
            requested_repo=requested_repo,
        )

    async def _retry_and_enqueue(self, *, dag_id: str, node_key: str):
        await self._repository.retry_dag_node(dag_id=dag_id, node_key=node_key)
        dag = await self._repository.get_task_dag(dag_id)
        if dag is None:
            raise LookupError(f"DAG {dag_id} not found")
        node = _node_from_dag(dag, node_key)
        queued = await self.enqueue_ready_nodes(dag=dag, task=dag.task, ready_nodes=[node])
        return queued[0] if queued else node

    def _should_retry_node(
        self,
        *,
        node_metadata: dict[str, object],
        external_metadata: dict[str, object],
        verification: CompletionVerification,
        quality_gate: QualityGateResult,
    ) -> bool:
        if _external_failure(external_metadata):
            return False
        if verification.satisfied and quality_gate.satisfied:
            return False
        if verification.followups:
            return False
        execution_mode = normalize_execution_mode(node_metadata.get("execution_mode"))
        if execution_mode != WRITE_PR:
            return False
        retry_count = node_metadata.get("retry_count")
        retry_count = retry_count if isinstance(retry_count, int) else 0
        return retry_count < self._settings.agent_write_max_model_retries

    async def _record_synced_task_usage(
        self,
        *,
        task_id: str,
        dag_id: str | None,
        node_key: str | None,
        external_task,
    ) -> None:
        usage = (external_task.metadata or {}).get("llm_observability")
        if not isinstance(usage, dict):
            return
        source = "task_orchestrator.read_dag_node"
        source_id = external_task.external_task_id
        existing_artifacts = await self._repository.list_task_artifacts(
            task_id=task_id,
            kind=LLM_COST_LEDGER_ARTIFACT_KIND,
            dag_id=dag_id,
            node_key=node_key,
        )
        for artifact in existing_artifacts:
            content = getattr(artifact, "content_json", None)
            if not isinstance(content, dict):
                continue
            if content.get("source") == source and content.get("source_id") == source_id:
                return
        await record_llm_cost_ledger(
            repository=self._repository,
            task_id=task_id,
            usage=usage,
            source=source,
            source_id=source_id,
            dag_id=dag_id,
            node_key=node_key,
            metadata={
                "provider": getattr(self._task_orchestrator, "provider", None),
            },
        )


def node_status_from_orchestrator(status: str) -> str:
    return {
        "pending": "queued",
        "queued": "queued",
        "dispatched": "running",
        "running": "running",
        "needs_input": "needs_input",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }.get(status, status)


def _synced_node_status(
    *,
    status_from_orchestrator: str,
    verification: CompletionVerification | None,
    quality_gate: QualityGateResult | None,
    external_metadata: dict[str, object] | None = None,
) -> str:
    if status_from_orchestrator == "completed" and _external_failure(external_metadata or {}):
        return "blocked_external"
    if (
        verification is not None
        and (not verification.satisfied or quality_gate is not None and not quality_gate.satisfied)
        and not verification.followups
    ):
        return "needs_changes"
    return status_from_orchestrator


def _user_state_metadata(
    *,
    node,
    status: str,
    status_from_orchestrator: str | None,
    verification: CompletionVerification | None,
    quality_gate: QualityGateResult | None,
    external_metadata: dict[str, object],
) -> dict[str, object]:
    failure = _external_failure(external_metadata)
    if failure:
        return {
            "user_status": "blocked_external",
            "status_reason": failure["reason"],
            "status_detail": failure["detail"],
            "next_action": failure["next_action"],
        }
    if status == "needs_changes":
        missing: list[str] = []
        if verification is not None and not verification.satisfied:
            missing.extend(verification.missing)
        if quality_gate is not None and not quality_gate.satisfied:
            missing.extend(quality_gate.missing)
        return {
            "user_status": "needs_changes",
            "status_reason": "quality_gate_blocked",
            "status_detail": "; ".join(_dedupe(missing))
            or ("The node completed, but required completion evidence is missing."),
            "next_action": (
                "Continue or retry the same DAG node until the PR, expected branch, "
                "and required test evidence are present."
            ),
        }
    if status in {"queued", "running"}:
        return {
            "user_status": status,
            "status_reason": f"node_{status}",
            "status_detail": f"DAG node `{node.node_key}` is {status}.",
            "next_action": "Wait for the node to finish or sync its orchestrator state.",
        }
    if status == "completed":
        return {
            "user_status": "completed",
            "status_reason": "node_completed",
            "status_detail": f"DAG node `{node.node_key}` completed.",
            "next_action": "The platform will queue dependent DAG nodes when dependencies are met.",
        }
    return {
        "user_status": status,
        "status_reason": f"node_{status}",
        "status_detail": f"DAG node `{node.node_key}` is {status}.",
        "next_action": None,
    }


def _external_failure(metadata: dict[str, object]) -> dict[str, str] | None:
    text = " ".join(
        value.lower()
        for value in (
            _str(metadata.get("multica_failure_reason")),
            _str(metadata.get("multica_error")),
            _str(metadata.get("result_output")),
        )
        if value
    )
    if not text:
        return None
    if "exceeded your current quota" in text or "insufficient_quota" in text:
        return {
            "reason": "openai_quota_exceeded",
            "detail": (
                "The model provider rejected the run because the configured OpenAI key "
                "has no available quota."
            ),
            "next_action": "Fix the OpenAI key/quota, then retry this DAG node.",
        }
    if "rate limit reached" in text or "tokens per min" in text:
        return {
            "reason": "model_rate_limited",
            "detail": "The model provider rate-limited the run before completion.",
            "next_action": "Retry this DAG node after the model rate limit window clears.",
        }
    if "checked-out workdir is empty" in text or "not a git repository" in text:
        return {
            "reason": "repo_checkout_failed",
            "detail": "The runtime could not get a usable repository checkout for this node.",
            "next_action": "Fix runtime repository checkout/sync, then retry this DAG node.",
        }
    if "api call failed after" in text:
        return {
            "reason": "model_provider_failed",
            "detail": "The model provider call failed before the agent could complete the node.",
            "next_action": "Inspect provider configuration and retry this DAG node.",
        }
    return None


def _node_status_notification(
    *,
    task_external_id: str | None,
    node_key: str,
    status: str,
    metadata: dict[str, object],
) -> dict[str, str] | None:
    user_status = _str(metadata.get("user_status")) or status
    reason = _str(metadata.get("status_reason"))
    detail = _str(metadata.get("status_detail"))
    next_action = _str(metadata.get("next_action"))
    key = f"{node_key}:{user_status}:{reason or ''}"
    if user_status in {"queued", "running"}:
        body = f"DAG node `{node_key}` is `{user_status}` for `{task_external_id or 'task'}`."
    elif user_status == "completed":
        body = f"DAG node `{node_key}` completed for `{task_external_id or 'task'}`."
    elif user_status in {"blocked_external", "needs_changes", "failed", "needs_input"}:
        lines = [
            f"DAG node `{node_key}` is `{user_status}` for `{task_external_id or 'task'}`.",
        ]
        if reason:
            lines.append(f"Reason: `{reason}`.")
        if detail:
            lines.append(detail)
        if next_action:
            lines.append(f"Next action: {next_action}")
        body = "\n".join(lines)
    else:
        return None
    return {"key": key, "body": body}


def _linear_issue_id(metadata: dict[str, object]) -> str | None:
    for key in ("linear_issue_id", "issue_id"):
        value = _str(metadata.get(key))
        if value:
            return value
    for key in ("linear_task", "user_intent"):
        value = metadata.get(key)
        if isinstance(value, dict):
            issue_id = _str(value.get("issue_id"))
            if issue_id:
                return issue_id
    return None


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _quality_feedback(
    *,
    verification: CompletionVerification,
    quality_gate: QualityGateResult,
) -> dict[str, object]:
    return {
        "verification_status": verification.status,
        "verification_missing": list(verification.missing),
        "quality_gate_status": quality_gate.status,
        "quality_gate_missing": list(quality_gate.missing),
        "instruction": (
            "Continue this same DAG node until all missing verification and quality "
            "gate items are fixed. Do not ask the human to choose an option."
        ),
    }


def _dag_node_idempotency_key(
    *,
    dag_id: str,
    node_key: str,
    metadata: dict[str, object],
) -> str:
    retry_count = metadata.get("retry_count")
    attempt = retry_count if isinstance(retry_count, int) else 0
    return f"{dag_id}:{node_key}:{attempt}"


def _node_from_dag(dag, node_key: str):
    node = next((node for node in dag.nodes if node.node_key == node_key), None)
    if node is None:
        raise LookupError(f"DAG node {node_key} not found")
    return node
