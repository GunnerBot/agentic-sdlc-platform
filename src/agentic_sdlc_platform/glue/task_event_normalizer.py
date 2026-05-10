import re
from dataclasses import dataclass

from agentic_sdlc_platform.glue.execution_policy import DRY_RUN


@dataclass(frozen=True)
class NormalizedTaskEvent:
    source: str
    external_id: str
    title: str
    issue_id: str | None = None
    repo: str | None = None
    dag_template: str | None = None
    url: str | None = None
    body: str | None = None
    execution_mode: str = DRY_RUN


@dataclass(frozen=True)
class NormalizedTaskUpdate:
    source: str
    external_id: str
    status: str
    repo: str | None = None
    metadata: dict[str, object] | None = None
    dag_id: str | None = None
    dag_node_key: str | None = None


class TaskEventNormalizer:
    def __init__(
        self,
        linear_agent_user_id: str | None = None,
        default_execution_mode: str = DRY_RUN,
    ) -> None:
        self._linear_agent_user_id = linear_agent_user_id
        self._default_execution_mode = default_execution_mode

    def normalize(
        self,
        source: str,
        event_type: str,
        payload: dict[str, object],
    ) -> NormalizedTaskEvent | None:
        if source == "linear":
            return self._normalize_linear(event_type=event_type, payload=payload)
        if source == "github":
            return self._normalize_github(event_type=event_type, payload=payload)
        return None

    def normalize_update(
        self,
        source: str,
        event_type: str,
        payload: dict[str, object],
    ) -> NormalizedTaskUpdate | None:
        if source == "github" and event_type == "pull_request":
            return self._normalize_github_pull_request_update(payload)
        return None

    def _normalize_linear(
        self,
        event_type: str,
        payload: dict[str, object],
    ) -> NormalizedTaskEvent | None:
        if event_type != "Issue" and payload.get("type") != "Issue":
            return None

        data = _dict_value(payload.get("data"))
        if self._linear_agent_user_id and not self._matches_linear_assignee(data):
            return None

        title = _str_value(data.get("title"))
        if not title:
            return None

        external_id = _str_value(data.get("identifier")) or _str_value(data.get("id"))
        if not external_id:
            return None

        label_names = _linear_label_names(data)
        return NormalizedTaskEvent(
            source="linear",
            external_id=external_id,
            title=title,
            issue_id=_str_value(data.get("id")),
            repo=_repo_from_labels(label_names),
            dag_template=_dag_template_from_labels(label_names),
            url=_str_value(data.get("url")),
            body=_str_value(data.get("description")),
            execution_mode=self._default_execution_mode,
        )

    def _normalize_github(
        self,
        event_type: str,
        payload: dict[str, object],
    ) -> NormalizedTaskEvent | None:
        if event_type != "issues":
            return None

        issue = _dict_value(payload.get("issue"))
        repository = _dict_value(payload.get("repository"))
        label_names = _github_label_names(issue)
        if "agent" not in {label.lower() for label in label_names}:
            return None

        title = _str_value(issue.get("title"))
        repo = _str_value(repository.get("full_name"))
        number = issue.get("number")
        if not title or not repo or not isinstance(number, int):
            return None

        return NormalizedTaskEvent(
            source="github",
            external_id=f"{repo}#{number}",
            title=title,
            repo=repo,
            url=_str_value(issue.get("html_url")),
            body=_str_value(issue.get("body")),
            execution_mode=self._default_execution_mode,
        )

    def _matches_linear_assignee(self, data: dict[str, object]) -> bool:
        assignee = _dict_value(data.get("assignee"))
        return _str_value(assignee.get("id")) == self._linear_agent_user_id

    def _normalize_github_pull_request_update(
        self,
        payload: dict[str, object],
    ) -> NormalizedTaskUpdate | None:
        pull_request = _dict_value(payload.get("pull_request"))
        action = _str_value(payload.get("action"))
        external_id = _extract_ticket_key(
            _str_value(_dict_value(pull_request.get("head")).get("ref")),
            _str_value(pull_request.get("title")),
            _str_value(pull_request.get("body")),
        )
        dag_reference = _extract_dag_node_reference(
            _str_value(_dict_value(pull_request.get("head")).get("ref")),
            _str_value(pull_request.get("title")),
            _str_value(pull_request.get("body")),
        )
        status = _github_pull_request_status(action, pull_request.get("merged"))
        if not (external_id or dag_reference) or not status:
            return None
        if external_id is None and dag_reference is not None:
            external_id = f"{dag_reference[0]}:{dag_reference[1]}"

        metadata: dict[str, object] = {}
        head_branch = _str_value(_dict_value(pull_request.get("head")).get("ref"))
        if head_branch:
            metadata["head_branch"] = head_branch
        number = pull_request.get("number")
        if isinstance(number, int):
            metadata["pull_request"] = number
        url = _str_value(pull_request.get("html_url"))
        if url:
            metadata["url"] = url
        if dag_reference is not None:
            expected_reference = f"dag/{dag_reference[0]}/{dag_reference[1]}"
            metadata["pr_body_reference"] = expected_reference
            metadata["pr_body_has_dag_reference"] = (
                _extract_dag_node_reference(_str_value(pull_request.get("body")))
                == dag_reference
            )

        return NormalizedTaskUpdate(
            source="github",
            external_id=external_id,
            status=status,
            repo=_str_value(_dict_value(payload.get("repository")).get("full_name")),
            metadata=metadata,
            dag_id=dag_reference[0] if dag_reference else None,
            dag_node_key=dag_reference[1] if dag_reference else None,
        )


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _str_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _linear_label_names(data: dict[str, object]) -> list[str]:
    labels = _dict_value(data.get("labels"))
    nodes = labels.get("nodes")
    if not isinstance(nodes, list):
        return []
    return [
        label_name
        for node in nodes
        if isinstance(node, dict)
        if (label_name := _str_value(_dict_value(node).get("name"))) is not None
    ]


def _github_label_names(issue: dict[str, object]) -> list[str]:
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return []
    return [
        label_name
        for label in labels
        if isinstance(label, dict)
        if (label_name := _str_value(_dict_value(label).get("name"))) is not None
    ]


def _repo_from_labels(label_names: list[str]) -> str | None:
    for label in label_names:
        if label and label.startswith("repo:"):
            return label.removeprefix("repo:")
    return None


def _dag_template_from_labels(label_names: list[str]) -> str | None:
    aliases = {
        "bug": "bugfix",
        "bugfix": "bugfix",
        "feature": "feature",
        "refactor": "refactor",
        "security": "security",
    }
    for label in label_names:
        normalized = label.strip().lower()
        if normalized.startswith("type:"):
            normalized = normalized.removeprefix("type:")
        if normalized.startswith("template:"):
            normalized = normalized.removeprefix("template:")
        if normalized in aliases:
            return aliases[normalized]
    return None


def _extract_ticket_key(*candidates: str | None) -> str | None:
    for candidate in candidates:
        if not candidate:
            continue
        match = re.search(r"\b[A-Z][A-Z0-9]+-\d+\b", candidate)
        if match:
            return match.group(0)
    return None


def _extract_dag_node_reference(*candidates: str | None) -> tuple[str, str] | None:
    uuid_pattern = (
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    )
    node_pattern = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"
    patterns = [
        rf"\bdag[:/]({uuid_pattern})[:/]({node_pattern})",
        rf"\bdag-({uuid_pattern})-({node_pattern})",
        rf"\b({uuid_pattern}):({node_pattern})",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        for pattern in patterns:
            match = re.search(pattern, candidate)
            if match:
                return match.group(1), match.group(2).rstrip(".,);]}")
    return None


def _github_pull_request_status(action: str | None, merged: object) -> str | None:
    if action in {"opened", "reopened", "synchronize"}:
        return "pr_open"
    if action == "ready_for_review":
        return "in_review"
    if action == "closed":
        return "merged" if merged is True else "pr_closed"
    return None
