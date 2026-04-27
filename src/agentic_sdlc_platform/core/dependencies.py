from agentic_sdlc_platform.adapters.claude import ClaudeModelProvider
from agentic_sdlc_platform.adapters.graphify import GraphifyGraphStore
from agentic_sdlc_platform.adapters.hermes import HermesAgentAdapter
from agentic_sdlc_platform.adapters.linear import LinearIssueAdapter
from agentic_sdlc_platform.adapters.local_executor import LocalAgentExecutor
from agentic_sdlc_platform.adapters.multica import MulticaTaskOrchestrator
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.glue.channel_budget import ChannelBudgetLedger
from agentic_sdlc_platform.glue.channel_mapping import ChannelAuthorizer, load_channel_authorizer
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.persistence.session import build_session_factory
from agentic_sdlc_platform.ports.agent_executor import AgentExecutorPort
from agentic_sdlc_platform.ports.graph_store import GraphStorePort
from agentic_sdlc_platform.ports.hermes_session import HermesSessionPort
from agentic_sdlc_platform.ports.issue_tracker import IssueTrackerPort
from agentic_sdlc_platform.ports.model_provider import ModelProviderPort
from agentic_sdlc_platform.ports.task_orchestrator import TaskOrchestratorPort


def build_model_provider(settings: Settings) -> ModelProviderPort:
    return ClaudeModelProvider(settings)


def build_graph_store(settings: Settings) -> GraphStorePort:
    return GraphifyGraphStore(settings)


def build_repository(settings: Settings) -> PersistenceRepository:
    return PersistenceRepository(build_session_factory(settings))


def build_task_orchestrator(settings: Settings) -> TaskOrchestratorPort | None:
    if not settings.multica_http_enabled:
        return None
    return MulticaTaskOrchestrator(settings)


def build_agent_executor(settings: Settings) -> AgentExecutorPort | None:
    if not settings.agent_executor_enabled:
        return None
    return LocalAgentExecutor(settings)


def build_hermes_session(settings: Settings) -> HermesSessionPort | None:
    if not settings.hermes_http_enabled:
        return None
    return HermesAgentAdapter(settings)


def build_channel_authorizer(settings: Settings) -> ChannelAuthorizer:
    return load_channel_authorizer(settings.channel_mapping_path)


def build_channel_budget_ledger(settings: Settings) -> ChannelBudgetLedger:
    return ChannelBudgetLedger(
        cap_usd=settings.channel_cost_cap_usd,
        default_request_cost_usd=settings.channel_default_request_cost_usd,
    )


def build_issue_tracker(settings: Settings) -> IssueTrackerPort | None:
    if not settings.linear_http_enabled:
        return None
    return LinearIssueAdapter(settings)
