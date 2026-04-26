from dataclasses import dataclass


@dataclass(frozen=True)
class DeployRequest:
    repo: str
    branch: str
    sha: str


class DeployHook:
    async def trigger(self, request: DeployRequest) -> bool:
        return request.branch == "agent-staging"
