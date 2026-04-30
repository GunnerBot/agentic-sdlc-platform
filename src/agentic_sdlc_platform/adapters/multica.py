import asyncio
import json
import re
from collections.abc import Awaitable, Callable

import httpx

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.glue.execution_policy import (
    code_generation_policy,
    github_write_enabled,
    normalize_execution_mode,
    retry_policy_for_mode,
    sanitize_write_metadata,
)
from agentic_sdlc_platform.glue.llm_observability import estimated_llm_usage
from agentic_sdlc_platform.ports.task_orchestrator import (
    TaskCommentRequest,
    TaskCommentResponse,
    TaskConversationMessage,
    TaskOrchestratorError,
    TaskReadRequest,
    TaskRequest,
    TaskResponse,
    TaskUpdateRequest,
)


class MulticaTaskOrchestrator:
    provider = "multica"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._sleep = sleep

    async def create_task(self, request: TaskRequest) -> TaskResponse:
        metadata = request.metadata or {}
        runtime_provider = self._runtime_provider(metadata)
        agent = await self._ensure_agent(runtime_provider)
        description = self._issue_description(
            request=request,
            runtime_provider=runtime_provider,
            agent=agent,
        )
        token_observability = estimated_llm_usage(
            settings=self._settings,
            model=runtime_provider,
            operation="multica.create_task.description",
            input_text=description,
        )

        response = await self._request_with_retries(
            failure_message="multica create_task failed",
            method="POST",
            path="/api/issues",
            payload={
                "title": request.title,
                "description": description,
                "status": "todo",
                "priority": "none",
                "assignee_type": "agent",
                "assignee_id": _required_str(agent, "id", "multica agent"),
            },
        )

        issue = response.json()
        issue_id = _required_str(issue, "id", "multica issue")
        task = await self._read_issue_task(
            issue_id=issue_id,
            expected_agent_id=_required_str(agent, "id", "multica agent"),
        )
        task_id = _required_str(task, "id", "multica task")
        status = _required_str(task, "status", "multica task")
        runtime_id = _str(task.get("runtime_id")) or _str(agent.get("runtime_id"))
        response_metadata = {
            "multica_issue_id": issue_id,
            "multica_issue_status": _str(issue.get("status")),
            "multica_issue_key": _str(issue.get("key")) or _str(issue.get("identifier")),
            "multica_task_id": task_id,
            "multica_task_status": status,
            "multica_agent_id": _required_str(agent, "id", "multica agent"),
            "multica_agent_name": _str(agent.get("name")),
            "multica_runtime_id": runtime_id,
            "multica_runtime_provider": runtime_provider,
            "multica_workspace_id": self._settings.multica_workspace_id,
            "llm_observability": token_observability,
        }
        return TaskResponse(
            external_task_id=task_id,
            status=status,
            metadata={key: value for key, value in response_metadata.items() if value is not None},
        )

    async def update_task(self, request: TaskUpdateRequest) -> TaskResponse:
        metadata = request.metadata or {}
        issue_id = _str(metadata.get("multica_issue_id"))
        if issue_id:
            await self._request_with_retries(
                failure_message="multica update_task failed",
                method="POST",
                path=f"/api/issues/{issue_id}/comments",
                payload={
                    "type": "comment",
                    "content": self._status_comment(request),
                },
            )
            return TaskResponse(
                external_task_id=request.external_task_id,
                status=request.status,
                metadata={"multica_issue_id": issue_id, "multica_update_synced": True},
            )

        return await self.read_task(
            TaskReadRequest(
                external_task_id=request.external_task_id,
                metadata=metadata,
            )
        )

    async def read_task(self, request: TaskReadRequest) -> TaskResponse:
        metadata = request.metadata or {}
        issue_id = _str(metadata.get("multica_issue_id"))
        task: dict[str, object] | None = None
        if issue_id:
            task = await self._read_issue_task(
                issue_id=issue_id,
                expected_task_id=request.external_task_id,
            )
        if task is None:
            task = await self._read_task_from_agents(request.external_task_id)
        task_id = _required_str(task, "id", "multica task")
        status = _required_str(task, "status", "multica task")
        response_metadata = {
            "multica_issue_id": _str(task.get("issue_id")) or issue_id,
            "multica_task_id": task_id,
            "multica_task_status": status,
            "multica_agent_id": _str(task.get("agent_id")),
            "multica_runtime_id": _str(task.get("runtime_id")),
            "multica_failure_reason": _str(task.get("failure_reason")),
            "multica_error": _str(task.get("error")),
            "multica_attempt": (
                task.get("attempt") if isinstance(task.get("attempt"), int) else None
            ),
            "multica_completed_at": _str(task.get("completed_at")),
            "multica_started_at": _str(task.get("started_at")),
            "multica_dispatched_at": _str(task.get("dispatched_at")),
            "result_output": _task_result_output(task),
            **_task_result_pr_metadata(task),
        }
        execution_usage = _task_result_usage_metadata(task, self._settings)
        if execution_usage is not None:
            response_metadata["llm_observability"] = execution_usage
        return TaskResponse(
            external_task_id=task_id,
            status=status,
            metadata={key: value for key, value in response_metadata.items() if value is not None},
        )

    async def add_comment(self, request: TaskCommentRequest) -> TaskCommentResponse:
        metadata = request.metadata or {}
        issue_id = _str(metadata.get("multica_issue_id")) or _str(
            metadata.get("orchestrator_issue_id")
        )
        if issue_id is None:
            task = await self._read_task_from_agents(request.external_task_id)
            issue_id = _str(task.get("issue_id"))
        if issue_id is None:
            raise TaskOrchestratorError("multica issue ID is not available for comment")

        response = await self._request_with_retries(
            failure_message="multica add_comment failed",
            method="POST",
            path=f"/api/issues/{issue_id}/comments",
            payload={
                "type": "comment",
                "content": request.body,
            },
        )
        payload = _json_dict(response.json(), "multica comment")
        return TaskCommentResponse(
            external_task_id=request.external_task_id,
            comment_id=_str(payload.get("id")),
            status="commented",
            metadata={
                key: value
                for key, value in {
                    "multica_issue_id": issue_id,
                    "multica_comment_id": _str(payload.get("id")),
                    "multica_comment_actor": request.actor,
                }.items()
                if value is not None
            },
        )

    async def list_comments(
        self,
        external_task_id: str,
        metadata: dict[str, object] | None = None,
    ) -> list[TaskConversationMessage]:
        metadata = metadata or {}
        issue_id = _str(metadata.get("multica_issue_id")) or _str(
            metadata.get("orchestrator_issue_id")
        )
        if issue_id is None:
            task = await self._read_task_from_agents(external_task_id)
            issue_id = _str(task.get("issue_id"))
        if issue_id is None:
            raise TaskOrchestratorError("multica issue ID is not available for comments")

        response = await self._request_with_retries(
            failure_message="multica list_comments failed",
            method="GET",
            path=f"/api/issues/{issue_id}/comments",
            payload=None,
        )
        comments = _json_list(response.json(), "multica comments")
        messages = []
        for comment in comments:
            comment_id = _str(comment.get("id"))
            body = _str(comment.get("content")) or _str(comment.get("body"))
            if comment_id is None or body is None:
                continue
            messages.append(
                TaskConversationMessage(
                    id=comment_id,
                    body=body,
                    actor=_str(comment.get("author_id")) or _str(comment.get("actor")),
                    created_at=_str(comment.get("created_at")),
                    metadata={"multica_issue_id": issue_id},
                )
            )
        return messages

    async def _ensure_agent(self, runtime_provider: str) -> dict[str, object]:
        agent_name = f"{self._settings.multica_agent_name_prefix}-{runtime_provider}"
        agents_response = await self._request_with_retries(
            failure_message="multica list_agents failed",
            method="GET",
            path="/api/agents",
            payload=None,
        )
        agents = _json_list(agents_response.json(), "multica agents")
        runtimes = await self._list_runtimes()
        runtime_by_id = {
            runtime_id: runtime
            for runtime in runtimes
            if (runtime_id := _str(runtime.get("id"))) is not None
        }
        for agent in agents:
            runtime = runtime_by_id.get(_str(agent.get("runtime_id")) or "")
            if (
                _str(agent.get("name")) == agent_name
                and _str(agent.get("archived_at")) is None
                and _str(runtime.get("provider")) == runtime_provider
            ):
                return agent

        runtime = self._select_runtime(runtimes, runtime_provider)
        response = await self._request_with_retries(
            failure_message="multica create_agent failed",
            method="POST",
            path="/api/agents",
            payload={
                "name": agent_name,
                "description": "Agentic SDLC Platform runtime adapter agent.",
                "instructions": (
                    "Execute the assigned DAG node exactly as described. "
                    "Use the expected branch and PR reference from the issue description. "
                    "When running shell commands, prefix them with `rtk`. "
                    "For repository questions, prefer supplied indexed graph context before "
                    "broad codebase scans, then verify conclusions against checked-out source."
                ),
                "runtime_id": _required_str(runtime, "id", "multica runtime"),
                "visibility": "workspace",
                "max_concurrent_tasks": 1,
            },
        )
        return _json_dict(response.json(), "multica agent")

    async def _list_runtimes(self) -> list[dict[str, object]]:
        response = await self._request_with_retries(
            failure_message="multica list_runtimes failed",
            method="GET",
            path="/api/runtimes",
            payload=None,
        )
        return _json_list(response.json(), "multica runtimes")

    def _select_runtime(
        self,
        runtimes: list[dict[str, object]],
        runtime_provider: str,
    ) -> dict[str, object]:
        provider_runtimes = [
            runtime
            for runtime in runtimes
            if _str(runtime.get("provider")) == runtime_provider
        ]
        online = [
            runtime
            for runtime in provider_runtimes
            if _str(runtime.get("status")) == "online"
        ]
        selected = (online or provider_runtimes or [None])[0]
        if selected is None:
            raise TaskOrchestratorError(
                f"no multica runtime is registered for provider {runtime_provider!r}"
            )
        return selected

    async def _read_issue_task(
        self,
        issue_id: str,
        expected_agent_id: str | None = None,
        expected_task_id: str | None = None,
    ) -> dict[str, object]:
        response = await self._request_with_retries(
            failure_message="multica read_task failed",
            method="GET",
            path=f"/api/issues/{issue_id}/task-runs",
            payload=None,
        )
        tasks = _json_list(response.json(), "multica issue tasks")
        for task in tasks:
            if expected_task_id and _str(task.get("id")) != expected_task_id:
                continue
            if expected_agent_id and _str(task.get("agent_id")) != expected_agent_id:
                continue
            return task
        raise TaskOrchestratorError("multica issue did not expose a matching task run")

    async def _read_task_from_agents(self, task_id: str) -> dict[str, object]:
        response = await self._request_with_retries(
            failure_message="multica list_agents failed",
            method="GET",
            path="/api/agents",
            payload=None,
        )
        for agent in _json_list(response.json(), "multica agents"):
            agent_id = _str(agent.get("id"))
            if agent_id is None:
                continue
            tasks_response = await self._request_with_retries(
                failure_message="multica read_task failed",
                method="GET",
                path=f"/api/agents/{agent_id}/tasks",
                payload=None,
            )
            for task in _json_list(tasks_response.json(), "multica agent tasks"):
                if _str(task.get("id")) == task_id:
                    return task
        raise TaskOrchestratorError("multica task was not found")

    def _runtime_provider(self, metadata: dict[str, object]) -> str:
        for key in (
            "multica_runtime_provider",
            "runtime_provider",
            "preferred_runtime_provider",
            "agent_runtime_provider",
        ):
            value = _str(metadata.get(key))
            if value:
                return value
        return self._settings.multica_default_runtime_provider

    def _issue_description(
        self,
        *,
        request: TaskRequest,
        runtime_provider: str,
        agent: dict[str, object],
    ) -> str:
        metadata = request.metadata or {}
        execution_mode = normalize_execution_mode(
            metadata.get("execution_mode"),
            default=self._settings.agent_default_execution_mode,
        )
        github_write = github_write_enabled(execution_mode)
        retry_policy = retry_policy_for_mode(self._settings, execution_mode)
        payload_metadata = sanitize_write_metadata(
            metadata,
            execution_mode=execution_mode,
        )
        user_intent = _dict_value(metadata.get("user_intent"))
        execution_payload = {
            "source": request.source,
            "external_id": request.external_id,
            "repo": request.repo,
            "runtime_provider": runtime_provider,
            "execution_mode": execution_mode,
            "github_write_enabled": github_write,
            "retry_policy": retry_policy,
            "agent_id": _str(agent.get("id")),
            "agent_name": _str(agent.get("name")),
            "dag_id": metadata.get("dag_id"),
            "node_key": metadata.get("node_key"),
            "parent_task_id": metadata.get("parent_task_id"),
            "parent_external_id": metadata.get("parent_external_id"),
            "dependencies_completed": metadata.get("dependencies_completed"),
            "context_session_id": metadata.get("context_session_id"),
            "expected_pr_reference": metadata.get("expected_pr_reference")
            if github_write
            else None,
            "expected_branch": metadata.get("expected_branch") if github_write else None,
            "runtime_policy": {
                "shell_command_prefix": "rtk",
                "use_rtk_for_terminal_commands": True,
            },
            "code_generation_policy": metadata.get(
                "code_generation_policy",
                code_generation_policy(),
            ),
            "pr_plan": metadata.get("pr_plan"),
            "repo_context_policy": {
                "preferred_context_source": "graphify",
                "verify_graph_context_against_source": True,
                "avoid_repeated_broad_scans_when_indexed_context_is_available": True,
            },
            "metadata": payload_metadata,
        }
        user_intent_text = _user_intent_text(
            title=_str(user_intent.get("title")) or request.title,
            body=_str(user_intent.get("body")),
            url=_str(user_intent.get("url")),
        )
        acceptance_criteria_value = metadata.get("acceptance_criteria")
        acceptance_criteria = (
            [
                item
                for item in acceptance_criteria_value
                if isinstance(item, str) and item.strip()
            ]
            if isinstance(acceptance_criteria_value, list)
            else []
        )
        acceptance_text = (
            "\n".join(f"- {criterion}" for criterion in acceptance_criteria)
            if acceptance_criteria
            else "- Implement and verify the requested DAG node scope."
        )
        mode_lines = [
            f"Execution mode: {execution_mode}",
            f"GitHub write enabled: {str(github_write).lower()}",
            f"Max model retries: {retry_policy['max_model_retries']}",
        ]
        if not github_write:
            mode_lines.append("Repository writes are disabled for this execution mode.")
        return (
            f"Execute agentic SDLC task `{request.external_id}`.\n\n"
            f"Title: {request.title}\n"
            f"Repo: {request.repo or 'not specified'}\n"
            f"Runtime provider: {runtime_provider}\n"
            f"{chr(10).join(mode_lines)}\n\n"
            "Full user intent:\n"
            f"{user_intent_text}\n\n"
            "Runtime policy:\n"
            "- Prefix shell commands with `rtk`.\n"
            "- Use indexed repo context first when available; verify with source reads.\n\n"
            "Code generation policy:\n"
            "- Use trunk-based development: branch from the repo default/trunk branch.\n"
            "- Keep PRs small and ordered by the DAG dependency order shown in the payload.\n"
            "- When modifying shared/common/existing behavior, guard the change behind a "
            "feature flag or equivalent compatibility gate unless the task explicitly "
            "requires a breaking global change.\n"
            "- Put implementation and relevant tests in the same PR for the DAG node.\n\n"
            "Acceptance criteria for this DAG node:\n"
            f"{acceptance_text}\n\n"
            "Completion rule:\n"
            "- Do not report the task complete if any acceptance criterion remains as "
            "a next step or follow-up.\n"
            "- If a criterion is blocked by missing product/DB ownership detail, state "
            "the blocker explicitly instead of opening a partial PR as done.\n\n"
            "Execution payload:\n"
            "```json\n"
            f"{json.dumps(execution_payload, indent=2, sort_keys=True)}\n"
            "```"
        )

    def _status_comment(self, request: TaskUpdateRequest) -> str:
        metadata = request.metadata or {}
        compact_metadata = {
            key: value
            for key, value in metadata.items()
            if key
            in {
                "source",
                "event_type",
                "external_id",
                "dag_id",
                "node_key",
                "pr_url",
                "pr_number",
                "pr_state",
                "multica_issue_id",
            }
            and value is not None
        }
        return (
            f"agentic-sdlc-platform updated task `{request.external_task_id}` "
            f"to `{request.status}`.\n\n"
            "```json\n"
            f"{json.dumps(compact_metadata, indent=2, sort_keys=True)}\n"
            "```"
        )

    async def _request_with_retries(
        self,
        failure_message: str,
        method: str,
        path: str,
        payload: dict[str, object | None] | None,
    ) -> httpx.Response:
        if not self._settings.multica_http_enabled:
            raise TaskOrchestratorError("multica HTTP is disabled")
        if not self._settings.multica_base_url:
            raise TaskOrchestratorError("multica base URL is not configured")
        if not self._settings.multica_api_key:
            raise TaskOrchestratorError("multica API key is not configured")
        if not self._settings.multica_workspace_id:
            raise TaskOrchestratorError("multica workspace ID is not configured")

        attempts = self._settings.multica_max_retries + 1
        try:
            async with httpx.AsyncClient(
                base_url=self._settings.multica_base_url,
                timeout=self._settings.multica_timeout_seconds,
                transport=self._transport,
            ) as client:
                for attempt in range(attempts):
                    response = await client.request(
                        method,
                        path,
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {self._settings.multica_api_key}",
                            "X-Workspace-ID": self._settings.multica_workspace_id,
                            "X-Client-Platform": "agentic-sdlc-platform",
                        },
                    )
                    if response.status_code < 500:
                        response.raise_for_status()
                        return response
                    if attempt == attempts - 1:
                        response.raise_for_status()
                    await self._sleep(
                        self._settings.multica_retry_backoff_seconds * (2**attempt)
                    )
        except httpx.HTTPError as exc:
            raise TaskOrchestratorError(failure_message) from exc
        raise TaskOrchestratorError(failure_message)


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


_GITHUB_PR_URL_RE = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/(?P<number>\d+)"
)


def _task_result_pr_metadata(task: dict[str, object]) -> dict[str, object]:
    result = _dict_value(task.get("result"))
    pr_url = _str(result.get("pr_url"))
    pr_number = result.get("pr_number") if isinstance(result.get("pr_number"), int) else None
    if pr_url is None:
        output = _str(result.get("output"))
        if output:
            match = _GITHUB_PR_URL_RE.search(output)
            if match:
                pr_url = match.group(0)
                pr_number = int(match.group("number"))
    return {"pr_url": pr_url, "pr_number": pr_number}


def _task_result_output(task: dict[str, object]) -> str | None:
    result = _dict_value(task.get("result"))
    return _str(result.get("output"))


def _task_result_usage_metadata(
    task: dict[str, object],
    settings: Settings,
) -> dict[str, object] | None:
    result = _dict_value(task.get("result"))
    usage = _dict_value(result.get("llm_observability")) or _dict_value(
        result.get("usage")
    )
    if not usage:
        usage = _dict_value(task.get("llm_observability")) or _dict_value(task.get("usage"))
    if not usage:
        return None

    input_tokens = _int_value(usage.get("input_tokens")) or _int_value(
        usage.get("prompt_tokens")
    )
    output_tokens = _int_value(usage.get("output_tokens")) or _int_value(
        usage.get("completion_tokens")
    )
    total_tokens = _int_value(usage.get("total_tokens"))
    if input_tokens is None and output_tokens is not None and total_tokens is not None:
        input_tokens = max(total_tokens - output_tokens, 0)
    if output_tokens is None and input_tokens is not None and total_tokens is not None:
        output_tokens = max(total_tokens - input_tokens, 0)
    if input_tokens is None or output_tokens is None:
        return None

    model = _str(usage.get("model")) or _str(task.get("model")) or "hermes"
    operation = _str(usage.get("operation")) or "hermes.multica_task_execution"
    estimated = estimated_llm_usage(
        settings=settings,
        model=model,
        operation=operation,
        input_text=None,
        output_text=None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        estimation_method=_str(usage.get("estimation_method")) or "provider_usage",
    )
    explicit_cost = _float_value(usage.get("estimated_cost_usd"))
    if explicit_cost is not None:
        estimated["estimated_cost_usd"] = explicit_cost
        estimated["cost_source"] = "provider"
        estimated["provider_reported_cost"] = True
    elif "cost_source" in usage:
        estimated["cost_source"] = usage["cost_source"]
    for key in (
        "response_id",
        "response_ids",
        "session_id",
        "provider",
        "runtime_provider",
    ):
        if key in usage:
            estimated[key] = usage[key]
    return estimated


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _int_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _float_value(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _user_intent_text(*, title: str, body: str | None, url: str | None) -> str:
    lines = [f"Title: {title}"]
    if url:
        lines.append(f"URL: {url}")
    if body:
        lines.extend(["Body:", body])
    return "\n".join(lines)


def _json_dict(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TaskOrchestratorError(f"{label} response was not an object")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _json_list(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise TaskOrchestratorError(f"{label} response was not a list")
    return [_json_dict(item, label) for item in value]


def _required_str(payload: dict[str, object], key: str, label: str) -> str:
    value = _str(payload.get(key))
    if value is None:
        raise TaskOrchestratorError(f"{label} response did not include {key}")
    return value
