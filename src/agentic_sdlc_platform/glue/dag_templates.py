from agentic_sdlc_platform.glue.dag_decomposer import Subtask
from agentic_sdlc_platform.persistence.models import Task

TEMPLATE_NAMES = {"bugfix", "feature", "refactor", "security"}


def build_dag_template(template: str, task: Task) -> list[Subtask]:
    normalized = template.lower()
    if normalized == "bugfix":
        return _with_repo(
            task,
            [
                Subtask(id="reproduce", title=f"Reproduce {task.external_id}"),
                Subtask(
                    id="fix",
                    title=f"Implement fix for {task.external_id}",
                    depends_on=("reproduce",),
                ),
                Subtask(
                    id="test",
                    title=f"Add regression coverage for {task.external_id}",
                    depends_on=("fix",),
                ),
                Subtask(
                    id="review",
                    title=f"Review and prepare PR for {task.external_id}",
                    depends_on=("test",),
                ),
            ],
        )
    if normalized == "feature":
        return _with_repo(
            task,
            [
                Subtask(id="design", title=f"Design implementation for {task.external_id}"),
                Subtask(
                    id="contract",
                    title=f"Define contracts for {task.external_id}",
                    depends_on=("design",),
                ),
                Subtask(
                    id="implement",
                    title=f"Implement {task.external_id}",
                    depends_on=("contract",),
                ),
                Subtask(
                    id="verify",
                    title=f"Verify {task.external_id}",
                    depends_on=("implement",),
                ),
                Subtask(
                    id="review",
                    title=f"Review and prepare PR for {task.external_id}",
                    depends_on=("verify",),
                ),
            ],
        )
    if normalized == "refactor":
        return _with_repo(
            task,
            [
                Subtask(id="map", title=f"Map current behavior for {task.external_id}"),
                Subtask(
                    id="characterize",
                    title=f"Add characterization tests for {task.external_id}",
                    depends_on=("map",),
                ),
                Subtask(
                    id="refactor",
                    title=f"Refactor {task.external_id}",
                    depends_on=("characterize",),
                ),
                Subtask(
                    id="verify",
                    title=f"Verify refactor for {task.external_id}",
                    depends_on=("refactor",),
                ),
                Subtask(
                    id="review",
                    title=f"Review and prepare PR for {task.external_id}",
                    depends_on=("verify",),
                ),
            ],
        )
    if normalized == "security":
        return _with_repo(
            task,
            [
                Subtask(id="threat_model", title=f"Threat model {task.external_id}"),
                Subtask(
                    id="scan",
                    title=f"Scan affected code for {task.external_id}",
                    depends_on=("threat_model",),
                ),
                Subtask(
                    id="fix",
                    title=f"Implement security fix for {task.external_id}",
                    depends_on=("scan",),
                ),
                Subtask(
                    id="validate",
                    title=f"Validate security controls for {task.external_id}",
                    depends_on=("fix",),
                ),
                Subtask(
                    id="review",
                    title=f"Security review and prepare PR for {task.external_id}",
                    depends_on=("validate",),
                ),
            ],
        )
    raise ValueError(f"Unknown DAG template: {template}")


def _with_repo(task: Task, subtasks: list[Subtask]) -> list[Subtask]:
    return [
        Subtask(
            id=subtask.id,
            title=subtask.title,
            repo=task.repo,
            depends_on=subtask.depends_on,
        )
        for subtask in subtasks
    ]
