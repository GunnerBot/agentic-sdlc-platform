from agentic_sdlc_platform.glue.dag_decomposer import DagDecomposer, Subtask
from agentic_sdlc_platform.ports.model_provider import ModelRequest, ModelResponse


class FakeModelProvider:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(provider="fake", model="fake-model", content="[]")


class JsonModelProvider:
    def __init__(self, content: str) -> None:
        self.content = content

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(provider="fake", model="fake-model", content=self.content)


async def test_dag_decomposer_uses_model_provider_port_when_available() -> None:
    provider = FakeModelProvider()
    decomposer = DagDecomposer(model_provider=provider)

    subtasks = await decomposer.decompose("# Feature")

    assert [request.role for request in provider.requests] == ["plan_agent"]
    assert [subtask.id for subtask in subtasks] == ["implementation"]
    assert subtasks[0].acceptance_criteria == (
        "Requested behavior is implemented end to end.",
        "Relevant tests or documented verification are included in the same PR.",
    )


async def test_dag_decomposer_parses_model_json_subtasks() -> None:
    decomposer = DagDecomposer(
        model_provider=JsonModelProvider(
            """
[
  {"id": "api", "title": "Add API contract", "repo": "erp-api"},
  {"id": "web", "title": "Consume API", "repo": "erp-web", "depends_on": ["api"]}
]
"""
        )
    )

    subtasks = await decomposer.decompose("# Feature")

    assert subtasks == [
        Subtask(id="api", title="Add API contract", repo="erp-api"),
        Subtask(id="web", title="Consume API", repo="erp-web", depends_on=("api",)),
    ]


def test_dag_decomposer_exposes_json_parser_without_model_fallback() -> None:
    decomposer = DagDecomposer()

    assert decomposer.parse_subtasks("not json") == []
