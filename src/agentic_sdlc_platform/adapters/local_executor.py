from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.agent_executor import (
    AgentExecutionRequest,
    AgentExecutionResponse,
)


class LocalAgentExecutor:
    provider = "local"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def start_execution(
        self,
        request: AgentExecutionRequest,
    ) -> AgentExecutionResponse:
        workspace_root = self._settings.agent_executor_workspace_root.rstrip("/")
        workspace_path = (
            f"{workspace_root}/{request.dag_id}/{request.node_key}"
            if workspace_root
            else None
        )
        return AgentExecutionResponse(
            external_execution_id=f"local:{request.execution_id}",
            status="running",
            branch_name=request.branch_name,
            workspace_path=workspace_path,
            metadata={
                "mode": "stub",
                "expected_pr_reference": request.pr_reference,
                "codex_command": _codex_command(request, workspace_path),
            },
        )


def _codex_command(request: AgentExecutionRequest, workspace_path: str | None) -> str:
    workspace = workspace_path or "<workspace>"
    return (
        f"cd {workspace} && codex exec "
        f"'Implement DAG node {request.node_key} for task {request.task_id}. "
        f"Create branch {request.branch_name} and include {request.pr_reference} in the PR body.'"
    )
