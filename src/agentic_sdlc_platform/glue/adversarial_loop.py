from __future__ import annotations

import json
from dataclasses import dataclass, field

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.glue.adversarial_review import (
    adversarial_review_approved,
    adversarial_review_required,
    normalize_adversarial_review,
)
from agentic_sdlc_platform.glue.llm_observability import record_llm_cost_ledger
from agentic_sdlc_platform.ports.model_provider import (
    ModelProviderError,
    ModelProviderPort,
    ModelRequest,
)
from agentic_sdlc_platform.ports.task_orchestrator import (
    TaskCommentRequest,
    TaskOrchestratorPort,
)


@dataclass(frozen=True)
class AdversarialLoopResult:
    status: str
    node_metadata: dict[str, object] = field(default_factory=dict)

    @property
    def should_requeue(self) -> bool:
        return self.status == "revision_requested"

    @property
    def human_intervention_required(self) -> bool:
        return self.status == "escalated"


async def run_adversarial_review_loop(
    *,
    task,
    dag,
    node,
    repository,
    model_provider: ModelProviderPort | None,
    task_orchestrator: TaskOrchestratorPort | None,
    settings: Settings,
    external_metadata: dict[str, object],
) -> AdversarialLoopResult:
    node_metadata = dict(getattr(node, "metadata_json", {}) or {})
    if not _loop_enabled(settings, node_metadata):
        return AdversarialLoopResult(status="not_applicable")

    if model_provider is None:
        return await _persist_escalation(
            task=task,
            dag=dag,
            node=node,
            repository=repository,
            reason="adversarial reviewer is not configured",
            turn=_next_turn(node_metadata),
        )

    response = None
    try:
        response = await model_provider.complete(
            ModelRequest(
                role="review_agent",
                prompt=_review_prompt(
                    task=task,
                    dag=dag,
                    node=node,
                    node_metadata=node_metadata,
                    external_metadata=external_metadata,
                    turn=_next_turn(node_metadata),
                ),
                task_id=task.id,
                metadata=_model_metadata(settings, dag.id, node.node_key),
            )
        )
        parsed = _review_payload_from_model(response.content)
    except (ModelProviderError, RuntimeError, ValueError) as exc:
        return await _persist_escalation(
            task=task,
            dag=dag,
            node=node,
            repository=repository,
            reason=f"adversarial reviewer failed: {exc}",
            turn=_next_turn(node_metadata),
        )

    if response and response.usage:
        await record_llm_cost_ledger(
            repository=repository,
            task_id=task.id,
            dag_id=dag.id,
            node_key=node.node_key,
            usage=response.usage,
            source="model_provider.adversarial_review",
            source_id=response.request_id,
            metadata={"provider": response.provider, "model": response.model},
        )

    turn = _next_turn(node_metadata)
    normalized = normalize_adversarial_review(
        {
            "phase": "execution",
            "turn": turn,
            "reviewer": "model_review_agent",
            "review": parsed,
        },
        required=True,
    )
    max_turns = max(settings.adversarial_review_max_turns, 1)
    max_turns_reached = turn >= max_turns and not normalized["approved"]
    if max_turns_reached or normalized["status"] == "escalated":
        normalized = {
            **normalized,
            "status": "escalated",
            "approved": False,
            "summary": normalized.get("summary")
            or f"Adversarial review reached max turns ({max_turns}).",
        }

    artifact = await _persist_review(
        task=task,
        dag=dag,
        node=node,
        repository=repository,
        normalized=normalized,
        raw_review=parsed,
        external_metadata=external_metadata,
    )
    node_metadata_update = _node_metadata_update(
        normalized=normalized,
        artifact_id=artifact.id,
        turn=turn,
        max_turns=max_turns,
    )
    await repository.update_dag_node_metadata(
        dag_id=dag.id,
        node_key=node.node_key,
        metadata=node_metadata_update,
    )

    if adversarial_review_approved(node_metadata_update):
        return AdversarialLoopResult(
            status="approved",
            node_metadata=node_metadata_update,
        )
    if normalized["status"] == "escalated" or max_turns_reached:
        await _record_escalation_audit(
            task=task,
            dag=dag,
            node=node,
            repository=repository,
            normalized=normalized,
            turn=turn,
            max_turns=max_turns,
        )
        return AdversarialLoopResult(
            status="escalated",
            node_metadata={
                **node_metadata_update,
                "human_intervention_required": True,
                "human_intervention_reason": "adversarial_review_escalated",
            },
        )

    await _send_revision_feedback(
        task_orchestrator=task_orchestrator,
        node=node,
        normalized=normalized,
        turn=turn,
    )
    return AdversarialLoopResult(
        status="revision_requested",
        node_metadata=node_metadata_update,
    )


def _loop_enabled(settings: Settings, node_metadata: dict[str, object]) -> bool:
    return bool(
        settings.adversarial_review_loop_enabled
        or adversarial_review_required(node_metadata)
    )


def _next_turn(node_metadata: dict[str, object]) -> int:
    value = node_metadata.get("adversarial_review_turn_count")
    return (value if isinstance(value, int) else 0) + 1


def _model_metadata(settings: Settings, dag_id: str, node_key: str) -> dict[str, str]:
    metadata = {"dag_id": dag_id, "node_key": node_key}
    if settings.adversarial_review_model:
        metadata["model"] = settings.adversarial_review_model
    else:
        metadata["model"] = settings.openai_write_model
    return metadata


def _review_prompt(
    *,
    task,
    dag,
    node,
    node_metadata: dict[str, object],
    external_metadata: dict[str, object],
    turn: int,
) -> str:
    payload = {
        "task": {
            "id": task.id,
            "external_id": task.external_id,
            "title": task.title,
            "repo": task.repo,
        },
        "dag": {"id": dag.id},
        "node": {
            "key": node.node_key,
            "title": node.title,
            "repo": node.repo,
            "acceptance_criteria": node_metadata.get("acceptance_criteria", []),
        },
        "turn": turn,
        "runtime_result": external_metadata,
        "previous_adversarial_feedback": node_metadata.get(
            "latest_adversarial_feedback"
        ),
    }
    return (
        "You are an adversarial supervisor reviewing a completed implementation DAG node. "
        "Review only the supplied runtime result, PR/test metadata, acceptance criteria, "
        "and previous feedback. Return strict JSON with this shape: "
        '{"verdict":"approved|partially-approved|revise|escalated",'
        '"score":{"overall":0-10},"summary":"short summary",'
        '"issues":[{"id":"stable-id","blocking":true|false,'
        '"description":"specific issue"}]}.\n\n'
        "Approve only when there are no blocking issues, no unfinished follow-up work, "
        "and required tests/contract evidence are present in the metadata.\n\n"
        f"Review input:\n{json.dumps(payload, indent=2, sort_keys=True, default=str)}"
    )


def _review_payload_from_model(content: str) -> dict[str, object]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("review model returned non-JSON content") from exc
    if not isinstance(parsed, dict):
        raise ValueError("review model returned a non-object JSON payload")
    return parsed


async def _persist_review(
    *,
    task,
    dag,
    node,
    repository,
    normalized: dict[str, object],
    raw_review: dict[str, object],
    external_metadata: dict[str, object],
):
    return await repository.create_task_artifact(
        task_id=task.id,
        dag_id=dag.id,
        node_key=node.node_key,
        kind="adversarial_review",
        name=f"{node.node_key}:adversarial-review:turn-{normalized['turn']}",
        content={
            "phase": "execution",
            "turn": normalized["turn"],
            "reviewer": "model_review_agent",
            "review": raw_review,
            "runtime_result": _bounded_metadata(external_metadata),
        },
        metadata=normalized,
    )


async def _persist_escalation(
    *,
    task,
    dag,
    node,
    repository,
    reason: str,
    turn: int,
) -> AdversarialLoopResult:
    normalized = {
        "required": True,
        "status": "escalated",
        "phase": "execution",
        "turn": turn,
        "reviewer": "platform",
        "checkpoint_id": None,
        "score": None,
        "summary": reason,
        "blocking_issue_count": 1,
        "blocking_issues": [{"id": "reviewer-unavailable", "description": reason}],
        "approved": False,
    }
    artifact = await _persist_review(
        task=task,
        dag=dag,
        node=node,
        repository=repository,
        normalized=normalized,
        raw_review={"verdict": "escalated", "summary": reason},
        external_metadata={},
    )
    update = {
        **_node_metadata_update(
            normalized=normalized,
            artifact_id=artifact.id,
            turn=turn,
            max_turns=turn,
        ),
        "human_intervention_required": True,
        "human_intervention_reason": "adversarial_reviewer_unavailable",
    }
    await repository.update_dag_node_metadata(
        dag_id=dag.id,
        node_key=node.node_key,
        metadata=update,
    )
    return AdversarialLoopResult(status="escalated", node_metadata=update)


def _node_metadata_update(
    *,
    normalized: dict[str, object],
    artifact_id: str,
    turn: int,
    max_turns: int,
) -> dict[str, object]:
    review = {**normalized, "artifact_id": artifact_id}
    return {
        "adversarial_review_required": True,
        "adversarial_review": review,
        "adversarial_review_turn_count": turn,
        "adversarial_review_max_turns": max_turns,
        "latest_adversarial_feedback": {
            "status": normalized["status"],
            "summary": normalized.get("summary"),
            "blocking_issues": normalized.get("blocking_issues", []),
            "turn": turn,
        },
    }


async def _send_revision_feedback(
    *,
    task_orchestrator: TaskOrchestratorPort | None,
    node,
    normalized: dict[str, object],
    turn: int,
) -> None:
    if task_orchestrator is None or not node.orchestrator_task_id:
        return
    add_comment = getattr(task_orchestrator, "add_comment", None)
    if not callable(add_comment):
        return
    try:
        await add_comment(
            TaskCommentRequest(
                external_task_id=node.orchestrator_task_id,
                body=_feedback_body(node.node_key, normalized),
                actor="adversarial-reviewer",
                metadata={
                    "dag_id": node.dag_id,
                    "node_key": node.node_key,
                    "review_status": str(normalized["status"]),
                    "review_turn": turn,
                },
            )
        )
    except Exception:
        return


def _feedback_body(node_key: str, normalized: dict[str, object]) -> str:
    summary = normalized.get("summary")
    lines = [f"Adversarial review requested changes for DAG node {node_key}:"]
    if isinstance(summary, str) and summary.strip():
        lines.append(summary.strip())
    issues = normalized.get("blocking_issues")
    if isinstance(issues, list) and issues:
        lines.extend(["", "Blocking issues:"])
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            issue_id = issue.get("id")
            description = issue.get("description")
            if isinstance(issue_id, str) and isinstance(description, str):
                lines.append(f"- {issue_id}: {description}")
            elif isinstance(description, str):
                lines.append(f"- {description}")
    return "\n".join(lines)


def _bounded_metadata(value: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(item, str):
            result[key] = item[:8000]
        elif isinstance(item, int | float | bool) or item is None:
            result[key] = item
        elif isinstance(item, list):
            result[key] = item[:20]
        elif isinstance(item, dict):
            result[key] = _bounded_metadata(item)
    return result


async def _record_escalation_audit(
    *,
    task,
    dag,
    node,
    repository,
    normalized: dict[str, object],
    turn: int,
    max_turns: int,
) -> None:
    await repository.record_audit_event(
        action="adversarial_review.escalated",
        actor="system",
        target_type="task_dag",
        target_id=dag.id,
        metadata={
            "task_id": task.id,
            "node_key": node.node_key,
            "turn": turn,
            "max_turns": max_turns,
            "summary": normalized.get("summary"),
            "blocking_issue_count": normalized.get("blocking_issue_count"),
        },
    )
