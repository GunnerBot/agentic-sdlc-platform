from agentic_sdlc_platform.adapters.claude import ClaudeModelProvider
from agentic_sdlc_platform.adapters.graphify import GraphifyGraphStore
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.persistence.session import build_session_factory
from agentic_sdlc_platform.ports.graph_store import GraphStorePort
from agentic_sdlc_platform.ports.model_provider import ModelProviderPort


def build_model_provider(settings: Settings) -> ModelProviderPort:
    return ClaudeModelProvider(settings)


def build_graph_store(settings: Settings) -> GraphStorePort:
    return GraphifyGraphStore(settings)


def build_repository(settings: Settings) -> PersistenceRepository:
    return PersistenceRepository(build_session_factory(settings))
