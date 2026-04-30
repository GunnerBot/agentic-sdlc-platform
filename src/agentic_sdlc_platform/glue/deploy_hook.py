from dataclasses import dataclass

from agentic_sdlc_platform.glue.auto_merge_gate import TRUNK_BRANCHES


@dataclass(frozen=True)
class DeployRequest:
    repo: str
    branch: str
    sha: str


class DeployHook:
    async def trigger(self, request: DeployRequest) -> bool:
        return request.branch in TRUNK_BRANCHES
