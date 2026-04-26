import schemathesis
from hypothesis import settings

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings

schema = schemathesis.openapi.from_asgi("/openapi.json", create_app(Settings()))


@schema.parametrize()
@settings(max_examples=25, deadline=None)
def test_openapi_contract(case: schemathesis.Case) -> None:
    case.call_and_validate()
