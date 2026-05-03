from agentic_sdlc_platform.glue.auto_merge_gate import AutoMergeGate, PullRequestState
from agentic_sdlc_platform.glue.channel_router import ChannelMessage, ChannelRouter, RouteTarget
from agentic_sdlc_platform.glue.cost_router import CostRouter
from agentic_sdlc_platform.glue.deploy_hook import DeployHook, DeployRequest


def test_channel_router_routes_ticket_commands_to_multica() -> None:
    router = ChannelRouter()

    route = router.route(ChannelMessage(channel="slack", text="/implement ENG-123", sender_id="u1"))

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
            base_branch="main",
        )
    )
    assert not gate.can_merge(
        PullRequestState(
            ci_green=True,
            critic_approved=True,
            human_approved=True,
            base_branch="agent-staging",
        )
    )


def test_cost_router_uses_standard_model_for_critic_default() -> None:
    route = CostRouter().route("critic_agent")

    assert route.provider == "openai"
    assert route.model == "gpt-5"


def test_cost_router_keeps_premium_model_as_explicit_escalation() -> None:
    route = CostRouter().route("premium_escalation_agent")

    assert route.provider == "openai"
    assert route.model == "gpt-5.5"


def test_cost_router_uses_low_cost_models_for_basic_roles() -> None:
    assert CostRouter().route("router_agent").model == "gpt-5-nano"
    assert CostRouter().route("plan_agent").model == "gpt-5-mini"


async def test_deploy_hook_follows_trunk_based_branches() -> None:
    hook = DeployHook()

    assert await hook.trigger(DeployRequest(repo="repo", branch="main", sha="abc"))
    assert not await hook.trigger(
        DeployRequest(repo="repo", branch="agent-staging", sha="abc")
    )
