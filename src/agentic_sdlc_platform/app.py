import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agentic_sdlc_platform.adapters.slack import SlackClient
from agentic_sdlc_platform.adapters.telegram import TelegramClient
from agentic_sdlc_platform.api.channels import router as channel_router
from agentic_sdlc_platform.api.health import router as health_router
from agentic_sdlc_platform.api.repos import router as repo_router
from agentic_sdlc_platform.api.slack import router as slack_router
from agentic_sdlc_platform.api.tasks import router as task_router
from agentic_sdlc_platform.api.telegram import router as telegram_router
from agentic_sdlc_platform.api.webhooks import router as webhook_router
from agentic_sdlc_platform.core.config import Settings, get_settings
from agentic_sdlc_platform.core.dependencies import (
    build_agent_executor,
    build_channel_authorizer,
    build_channel_budget_ledger,
    build_graph_store,
    build_hermes_session,
    build_issue_tracker,
    build_model_provider,
    build_repository,
    build_source_control,
    build_task_orchestrator,
)
from agentic_sdlc_platform.glue.conversation_sync import (
    ConversationSyncService,
    run_conversation_sync_loop,
)
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.agent_executor import AgentExecutorPort
from agentic_sdlc_platform.ports.graph_store import GraphStorePort
from agentic_sdlc_platform.ports.hermes_session import HermesSessionPort
from agentic_sdlc_platform.ports.issue_tracker import IssueTrackerPort
from agentic_sdlc_platform.ports.model_provider import ModelProviderPort
from agentic_sdlc_platform.ports.source_control import SourceControlPort
from agentic_sdlc_platform.ports.task_orchestrator import TaskOrchestratorPort


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await _start_conversation_sync_loop(app)
    try:
        yield
    finally:
        await _stop_conversation_sync_loop(app)


def create_app(
    settings: Settings | None = None,
    repository: PersistenceRepository | None = None,
    task_orchestrator: TaskOrchestratorPort | None = None,
    hermes_session: HermesSessionPort | None = None,
    model_provider: ModelProviderPort | None = None,
    graph_store: GraphStorePort | None = None,
    issue_tracker: IssueTrackerPort | None = None,
    agent_executor: AgentExecutorPort | None = None,
    source_control: SourceControlPort | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    app = FastAPI(
        title=resolved_settings.service_name,
        version=resolved_settings.version,
        docs_url="/docs" if resolved_settings.docs_enabled else None,
        redoc_url="/redoc" if resolved_settings.docs_enabled else None,
        lifespan=lifespan,
    )

    app.state.settings = resolved_settings
    app.state.model_provider = model_provider or build_model_provider(resolved_settings)
    app.state.graph_store = graph_store or build_graph_store(resolved_settings)
    app.state.repository = repository or build_repository(resolved_settings)
    app.state.task_orchestrator = task_orchestrator or build_task_orchestrator(resolved_settings)
    app.state.hermes_session = hermes_session or build_hermes_session(resolved_settings)
    app.state.channel_authorizer = build_channel_authorizer(resolved_settings)
    app.state.channel_budget_ledger = build_channel_budget_ledger(resolved_settings)
    app.state.issue_tracker = issue_tracker or build_issue_tracker(resolved_settings)
    app.state.agent_executor = agent_executor or build_agent_executor(resolved_settings)
    app.state.source_control = source_control or build_source_control(resolved_settings)
    app.state.slack_client = SlackClient(resolved_settings)
    app.state.telegram_client = TelegramClient(resolved_settings)
    app.state.conversation_sync_stop_event = None
    app.state.conversation_sync_task = None

    app.include_router(health_router)
    app.include_router(channel_router, prefix="/channels")
    app.include_router(repo_router, prefix="/repos")
    app.include_router(slack_router, prefix="/channels/slack")
    app.include_router(telegram_router, prefix="/channels/telegram")
    app.include_router(task_router, prefix="/tasks")
    app.include_router(webhook_router, prefix="/webhooks")
    return app


async def _start_conversation_sync_loop(app: FastAPI) -> None:
    if (
        not app.state.settings.conversation_sync_enabled
        or app.state.task_orchestrator is None
    ):
        return
    stop_event = asyncio.Event()
    app.state.conversation_sync_stop_event = stop_event
    app.state.conversation_sync_task = asyncio.create_task(
        run_conversation_sync_loop(
            service=ConversationSyncService(
                repository=app.state.repository,
                task_orchestrator=app.state.task_orchestrator,
                issue_tracker=app.state.issue_tracker,
                slack_client=app.state.slack_client,
                telegram_client=app.state.telegram_client,
            ),
            interval_seconds=app.state.settings.conversation_sync_interval_seconds,
            batch_size=app.state.settings.conversation_sync_batch_size,
            stop_event=stop_event,
        )
    )


async def _stop_conversation_sync_loop(app: FastAPI) -> None:
    stop_event = app.state.conversation_sync_stop_event
    sync_task = app.state.conversation_sync_task
    if stop_event is None or sync_task is None:
        return
    stop_event.set()
    sync_task.cancel()
    try:
        await sync_task
    except asyncio.CancelledError:
        pass
