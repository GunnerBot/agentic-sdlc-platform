from agentic_sdlc_platform.glue.auto_merge_gate import AutoMergeGate, PullRequestState
from agentic_sdlc_platform.glue.channel_router import ChannelMessage, ChannelRouter, RouteTarget
from agentic_sdlc_platform.glue.cost_router import CostRouter


def test_channel_router_routes_ticket_commands_to_multica() -> None:
    router = ChannelRouter()

    route = router.route(ChannelMessage(channel="slack", text="/implement OS-123", sender_id="u1"))

    assert route == RouteTarget.MULTICA_TASK


def test_channel_router_routes_questions_to_hermes_direct() -> None:
    router = ChannelRouter()

    route = router.route(
        ChannelMessage(channel="slack", text="How does FEFO work?", sender_id="u1")
    )

    assert route == RouteTarget.HERMES_DIRECT


def test_auto_merge_gate_only_allows_agent_staging_with_all_approvals() -> None:
    gate = AutoMergeGate()

    assert gate.can_merge(
        PullRequestState(
            ci_green=True,
            critic_approved=True,
            human_approved=True,
            base_branch="agent-staging",
        )
    )
    assert not gate.can_merge(
        PullRequestState(
            ci_green=True,
            critic_approved=True,
            human_approved=True,
            base_branch="main",
        )
    )


def test_cost_router_uses_cross_family_critic_default() -> None:
    route = CostRouter().route("critic_agent")

    assert route.provider == "openai"
    assert route.model == "gpt-5.4-mini"
