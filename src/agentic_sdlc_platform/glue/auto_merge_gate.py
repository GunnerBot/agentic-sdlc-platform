from dataclasses import dataclass


@dataclass(frozen=True)
class PullRequestState:
    ci_green: bool
    critic_approved: bool
    human_approved: bool
    base_branch: str


TRUNK_BRANCHES = {"main", "master", "trunk"}


class AutoMergeGate:
    def can_merge(self, state: PullRequestState) -> bool:
        return (
            state.base_branch in TRUNK_BRANCHES
            and state.ci_green
            and state.critic_approved
            and state.human_approved
        )
