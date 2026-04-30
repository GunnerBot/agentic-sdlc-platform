from __future__ import annotations

from math import ceil

from agentic_sdlc_platform.core.config import Settings

LLM_COST_LEDGER_ARTIFACT_KIND = "llm_cost_ledger"


def estimate_tokens(text: str | None, *, chars_per_token: float) -> int:
    if not text:
        return 0
    if chars_per_token <= 0:
        chars_per_token = 4.0
    return max(1, ceil(len(text) / chars_per_token))


def token_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    input_cost_per_million_usd: float,
    output_cost_per_million_usd: float,
) -> float:
    return round(
        (input_tokens / 1_000_000 * input_cost_per_million_usd)
        + (output_tokens / 1_000_000 * output_cost_per_million_usd),
        8,
    )


def estimated_llm_usage(
    *,
    settings: Settings,
    model: str,
    operation: str,
    input_text: str | None,
    output_text: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    estimation_method: str = "chars_per_token",
) -> dict[str, object]:
    resolved_input_tokens = (
        input_tokens
        if input_tokens is not None
        else estimate_tokens(
            input_text,
            chars_per_token=settings.observability_chars_per_token,
        )
    )
    resolved_output_tokens = (
        output_tokens
        if output_tokens is not None
        else estimate_tokens(
            output_text,
            chars_per_token=settings.observability_chars_per_token,
        )
    )
    resolved_total_tokens = (
        total_tokens
        if total_tokens is not None
        else resolved_input_tokens + resolved_output_tokens
    )
    return {
        "operation": operation,
        "model": model,
        "input_tokens": resolved_input_tokens,
        "output_tokens": resolved_output_tokens,
        "total_tokens": resolved_total_tokens,
        "estimated_cost_usd": token_cost_usd(
            input_tokens=resolved_input_tokens,
            output_tokens=resolved_output_tokens,
            input_cost_per_million_usd=settings.observability_input_cost_per_million_usd,
            output_cost_per_million_usd=settings.observability_output_cost_per_million_usd,
        ),
        "input_cost_per_million_usd": settings.observability_input_cost_per_million_usd,
        "output_cost_per_million_usd": settings.observability_output_cost_per_million_usd,
        "estimation_method": estimation_method,
    }


def usage_from_openai_payload(
    *,
    payload: dict[str, object],
    settings: Settings,
    model: str,
    operation: str,
    request_input_text: str | None,
    response_output_text: str | None,
) -> dict[str, object]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return estimated_llm_usage(
            settings=settings,
            model=model,
            operation=operation,
            input_text=request_input_text,
            output_text=response_output_text,
        )

    input_tokens = _int_value(usage.get("input_tokens")) or _int_value(
        usage.get("prompt_tokens")
    )
    output_tokens = _int_value(usage.get("output_tokens")) or _int_value(
        usage.get("completion_tokens")
    )
    total_tokens = _int_value(usage.get("total_tokens"))
    if input_tokens is None and output_tokens is not None and total_tokens is not None:
        input_tokens = max(total_tokens - output_tokens, 0)
    if output_tokens is None and input_tokens is not None and total_tokens is not None:
        output_tokens = max(total_tokens - input_tokens, 0)

    return estimated_llm_usage(
        settings=settings,
        model=model,
        operation=operation,
        input_text=request_input_text,
        output_text=response_output_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        estimation_method="provider_usage" if total_tokens is not None else "provider_partial",
    )


def _int_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def usage_records_from_metadata(
    metadata: dict[str, object],
    *,
    source: str,
    source_id: str | None = None,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    usage = metadata.get("llm_observability")
    if isinstance(usage, dict) and _is_usage_record(usage):
        records.append(
            {
                **usage,
                "source": source,
                "source_id": source_id,
            }
        )
    return records


async def record_llm_cost_ledger(
    *,
    repository,
    task_id: str,
    usage: dict[str, object] | None,
    source: str,
    source_id: str | None = None,
    dag_id: str | None = None,
    node_key: str | None = None,
    execution_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> object | None:
    if not isinstance(usage, dict) or not _is_usage_record(usage):
        return None
    return await repository.create_task_artifact(
        task_id=task_id,
        dag_id=dag_id,
        node_key=node_key,
        execution_id=execution_id,
        kind=LLM_COST_LEDGER_ARTIFACT_KIND,
        name=f"{source}:{source_id or 'unknown'}",
        content={
            **usage,
            "source": source,
            "source_id": source_id,
        },
        metadata=metadata or {},
    )


def usage_records_from_ledger_artifacts(artifacts: list[object]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for artifact in artifacts:
        content = getattr(artifact, "content_json", None)
        if not isinstance(content, dict) or not _is_usage_record(content):
            continue
        records.append(
            {
                **content,
                "source": _str_value(content.get("source")) or "ledger",
                "source_id": _str_value(content.get("source_id"))
                or getattr(artifact, "id", None),
            }
        )
    return records


def summarize_usage_records(records: list[dict[str, object]]) -> dict[str, object]:
    total_input_tokens = sum(_int_value(record.get("input_tokens")) or 0 for record in records)
    total_output_tokens = sum(_int_value(record.get("output_tokens")) or 0 for record in records)
    total_tokens = sum(_int_value(record.get("total_tokens")) or 0 for record in records)
    total_estimated_cost_usd = round(
        sum(_float_value(record.get("estimated_cost_usd")) or 0.0 for record in records),
        8,
    )
    enriched_records = enrich_usage_records(records)
    return {
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_tokens": total_tokens,
        "total_estimated_cost_usd": total_estimated_cost_usd,
        "exact_token_record_count": sum(
            1 for record in enriched_records if record.get("token_count_source") == "provider"
        ),
        "estimated_token_record_count": sum(
            1 for record in enriched_records if record.get("token_count_source") != "provider"
        ),
        "provider_cost_record_count": sum(
            1 for record in enriched_records if record.get("cost_source") == "provider"
        ),
    }


def enrich_usage_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    return [_with_precision_fields(record) for record in records]


def _with_precision_fields(record: dict[str, object]) -> dict[str, object]:
    estimation_method = _str_value(record.get("estimation_method"))
    token_count_source = (
        "provider"
        if estimation_method == "provider_usage"
        else "provider_partial"
        if estimation_method == "provider_partial"
        else "estimated"
    )
    cost_source = _str_value(record.get("cost_source"))
    if cost_source is None:
        cost_source = (
            "provider"
            if record.get("provider_reported_cost") is True
            else "configured_rate_estimate"
        )
    return {
        **record,
        "token_count_source": token_count_source,
        "cost_source": cost_source,
        "cost_exact": cost_source == "provider",
    }


def _is_usage_record(value: dict[str, object]) -> bool:
    return (
        _int_value(value.get("input_tokens")) is not None
        and _int_value(value.get("output_tokens")) is not None
        and _int_value(value.get("total_tokens")) is not None
        and _float_value(value.get("estimated_cost_usd")) is not None
    )


def _float_value(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _str_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
