from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizedTaskEvent:
    source: str
    external_id: str
    title: str
    repo: str | None = None
    url: str | None = None
    body: str | None = None


class TaskEventNormalizer:
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

    def _normalize_linear(
        self,
        event_type: str,
        payload: dict[str, object],
    ) -> NormalizedTaskEvent | None:
        if event_type != "Issue" and payload.get("type") != "Issue":
            return None

        data = _dict_value(payload.get("data"))
        title = _str_value(data.get("title"))
        if not title:
            return None

        external_id = _str_value(data.get("identifier")) or _str_value(data.get("id"))
        if not external_id:
            return None

        return NormalizedTaskEvent(
            source="linear",
            external_id=external_id,
            title=title,
            repo=_repo_from_labels(_linear_label_names(data)),
            url=_str_value(data.get("url")),
            body=_str_value(data.get("description")),
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
