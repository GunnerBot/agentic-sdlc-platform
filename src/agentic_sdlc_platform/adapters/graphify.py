from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.graph_store import (
    GraphIndexRequest,
    GraphIndexResult,
    GraphQuery,
    GraphQueryResult,
    GraphStoreError,
)


class GraphifyGraphStore:
    """Graphify graph store seam.

    Real endpoint paths and auth stay out until Graphify deployment mode is selected.
    """

    provider = "graphify"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def index(self, request: GraphIndexRequest) -> GraphIndexResult:
        self._ensure_configured()
        return GraphIndexResult(
            provider=self.provider,
            external_index_id=f"graphify:{request.repo}:{request.default_branch}",
            status="indexed",
        )

    async def query(self, request: GraphQuery) -> GraphQueryResult:
        self._ensure_configured()
        return GraphQueryResult(
            provider=self.provider,
            answer=f"graphify query accepted for repo={request.repo}",
            references=[self._settings.graphify_base_url or ""],
        )

    def _ensure_configured(self) -> None:
        if not self._settings.vendor_http_enabled:
            raise GraphStoreError("vendor HTTP is disabled")

        if not self._settings.graphify_base_url:
            raise GraphStoreError("graphify base URL is not configured")
