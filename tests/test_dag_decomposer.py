from agentic_sdlc_platform.glue.dag_decomposer import DagDecomposer
from agentic_sdlc_platform.ports.model_provider import ModelRequest, ModelResponse


class FakeModelProvider:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(provider="fake", model="fake-model", content="[]")


async def test_dag_decomposer_uses_model_provider_port_when_available() -> None:
    provider = FakeModelProvider()
    decomposer = DagDecomposer(model_provider=provider)

    subtasks = await decomposer.decompose("# Feature")

    assert [request.role for request in provider.requests] == ["plan_agent"]
    assert [subtask.id for subtask in subtasks] == ["scaffold"]
