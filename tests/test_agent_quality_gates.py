import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "enforce_agent_quality_gates.py"
SPEC = importlib.util.spec_from_file_location(
    "enforce_agent_quality_gates",
    SCRIPT_PATH,
)
assert SPEC is not None
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
evaluate_changed_files = MODULE.evaluate_changed_files


def test_agent_quality_gate_requires_tests_for_production_changes() -> None:
    result = evaluate_changed_files(["src/agentic_sdlc_platform/glue/quality_gate.py"])

    assert result.ok is False
    assert result.missing == (
        "Production code changed without same-PR tests. Add/update relevant tests.",
    )


def test_agent_quality_gate_requires_contract_tests_for_api_changes() -> None:
    result = evaluate_changed_files(
        [
            "src/agentic_sdlc_platform/api/tasks.py",
            "tests/test_task_dag_api.py",
        ]
    )

    assert result.ok is False
    assert result.missing == (
        "API/schema/webhook contract changed without same-PR Schemathesis contract tests.",
    )


def test_agent_quality_gate_passes_with_tests_and_contract_tests() -> None:
    result = evaluate_changed_files(
        [
            "src/agentic_sdlc_platform/api/tasks.py",
            "src/agentic_sdlc_platform/glue/quality_gate.py",
            "tests/test_quality_gate.py",
            "tests/contracts/test_openapi_contract.py",
        ]
    )

    assert result.ok is True
