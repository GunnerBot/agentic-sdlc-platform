from dataclasses import dataclass


@dataclass(frozen=True)
class PullRequestState:
    ci_green: bool
    critic_approved: bool
    human_approved: bool
    base_branch: str


class AutoMergeGate:
    def can_merge(self, state: PullRequestState) -> bool:
        return (
            state.base_branch == "agent-staging"
            and state.ci_green
            and state.critic_approved
            and state.human_approved
        )
