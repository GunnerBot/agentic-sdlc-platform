from agentic_sdlc_platform.glue.dag_templates import build_dag_template
from agentic_sdlc_platform.persistence.models import Task


def test_builtin_dag_templates_define_expected_execution_shapes() -> None:
    task = Task(
        id="task-1",
        inbound_event_id="event-1",
        source="linear",
        external_id="OS-1284",
        title="Build workflow templates",
        repo="keychain-os-erp",
    )

    templates = {
        "bugfix": ["reproduce", "fix", "test", "review"],
        "feature": ["design", "contract", "implement", "verify", "review"],
        "refactor": ["map", "characterize", "refactor", "verify", "review"],
        "security": ["threat_model", "scan", "fix", "validate", "review"],
    }

    for template, node_keys in templates.items():
        subtasks = build_dag_template(template, task)

        assert [subtask.id for subtask in subtasks] == node_keys
        assert {subtask.repo for subtask in subtasks} == {"keychain-os-erp"}
        assert subtasks[0].depends_on == ()
        assert subtasks[-1].id == "review"
        assert subtasks[-1].depends_on == (node_keys[-2],)
