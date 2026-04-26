import schemathesis
from hypothesis import HealthCheck, settings

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings


class FakeEvent:
    id = "event-1"


class FakeTask:
    id = "task-1"


class FakeWriteResult:
    event = FakeEvent()
    created = True


class FakeRepository:
    async def record_inbound_event(self, **kwargs):
        return FakeWriteResult()

    async def create_task_from_event(self, **kwargs):
        return FakeTask()

    async def record_audit_event(self, **kwargs):
        return None


schema = schemathesis.openapi.from_asgi(
    "/openapi.json",
    create_app(Settings(), repository=FakeRepository()),
)


@schema.parametrize()
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
)
def test_openapi_contract(case: schemathesis.Case) -> None:
    case.call_and_validate()
