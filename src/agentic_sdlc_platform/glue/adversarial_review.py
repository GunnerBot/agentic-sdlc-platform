from __future__ import annotations

APPROVED_REVIEW_STATUSES = {"approved", "partially_approved"}


def normalize_adversarial_review(
    payload: dict[str, object],
    *,
    required: bool,
) -> dict[str, object]:
    review = _dict(payload.get("review"))
    checkpoint = _dict(payload.get("checkpoint"))
    status = _normalize_status(
        review.get("verdict")
        or review.get("status")
        or payload.get("verdict")
        or payload.get("status")
        or "unknown"
    )
    blocking_issues = _blocking_issues(payload, review)
    blocking_issue_count = len(blocking_issues)
    score = _score(payload, review)
    summary = _str(payload.get("summary")) or _str(review.get("summary"))
    approved = status in APPROVED_REVIEW_STATUSES and blocking_issue_count == 0
    return {
        "required": required,
        "status": status,
        "phase": _str(payload.get("phase")),
        "turn": _int(payload.get("turn")),
        "reviewer": _str(payload.get("reviewer")),
        "checkpoint_id": _str(checkpoint.get("id")),
        "score": score,
        "summary": summary,
        "blocking_issue_count": blocking_issue_count,
        "blocking_issues": blocking_issues,
        "approved": approved,
    }


def adversarial_review_required(metadata: dict[str, object]) -> bool:
    review = _dict(metadata.get("adversarial_review"))
    return bool(
        metadata.get("adversarial_review_required") is True or review.get("required") is True
    )


def adversarial_review_approved(metadata: dict[str, object]) -> bool:
    review = _dict(metadata.get("adversarial_review"))
    if not review:
        return False
    status = _normalize_status(review.get("status"))
    blocking_issue_count = _int(review.get("blocking_issue_count"))
    if blocking_issue_count is None:
        blocking_issue_count = len(_list(review.get("blocking_issues")))
    approved_marker = review.get("approved") is True or status in APPROVED_REVIEW_STATUSES
    return approved_marker and blocking_issue_count == 0


def _blocking_issues(
    payload: dict[str, object],
    review: dict[str, object],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for value in _list(payload.get("blocking_issues")) + _list(review.get("blocking_issues")):
        issue = _issue_summary(value)
        if issue:
            issues.append(issue)
    for value in _list(review.get("issues")) + _list(payload.get("issues")):
        issue_value = _dict(value)
        if issue_value.get("blocking") is not True:
            continue
        issue = _issue_summary(value)
        if issue:
            issues.append(issue)
    return _dedupe_issues(issues)


def _issue_summary(value: object) -> dict[str, str]:
    if isinstance(value, str) and value.strip():
        return {"description": value.strip()}
    issue = _dict(value)
    if not issue:
        return {}
    result: dict[str, str] = {}
    issue_id = _str(issue.get("id"))
    description = (
        _str(issue.get("description"))
        or _str(issue.get("message"))
        or _str(issue.get("body"))
        or _str(issue.get("title"))
        or _str(issue.get("detail"))
    )
    if issue_id:
        result["id"] = issue_id
    if description:
        result["description"] = description
    return result


def _score(payload: dict[str, object], review: dict[str, object]) -> float | None:
    for value in (
        _dict(review.get("score")).get("overall"),
        review.get("score"),
        _dict(payload.get("score")).get("overall"),
        payload.get("score"),
    ):
        number = _number(value)
        if number is not None:
            return number
    return None


def _normalize_status(value: object) -> str:
    text = _str(value) or "unknown"
    return text.strip().lower().replace("-", "_").replace(" ", "_")


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _dedupe_issues(values: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, str]] = []
    for value in values:
        key = (value.get("id", ""), value.get("description", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
