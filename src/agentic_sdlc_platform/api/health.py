from fastapi import APIRouter, Request

from agentic_sdlc_platform.models.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/readyz", response_model=HealthResponse)
async def readyz() -> HealthResponse:
    return HealthResponse(status="ready")


@router.get("/ops/status")
async def ops_status(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    integrations = {
        "notion": _integration_status(
            enabled=settings.notion_http_enabled,
            required={"api_key": bool(settings.notion_api_key)},
        ),
        "google_docs": _integration_status(
            enabled=settings.google_docs_http_enabled,
            required={"bearer_token": bool(settings.google_docs_bearer_token)},
        ),
        "figma": _integration_status(
            enabled=settings.figma_http_enabled,
            required={"api_key": bool(settings.figma_api_key)},
        ),
        "design_image_hydration": _integration_status(
            enabled=settings.design_image_hydration_enabled,
            required={
                "vendor_http": settings.vendor_http_enabled,
                "openai_api_key": bool(settings.openai_api_key),
            },
        ),
        "multica": _integration_status(
            enabled=settings.multica_http_enabled,
            required={
                "base_url": bool(settings.multica_base_url),
                "api_key": bool(settings.multica_api_key),
                "workspace_id": bool(settings.multica_workspace_id),
            },
        ),
        "hermes": _integration_status(
            enabled=settings.hermes_http_enabled,
            required={
                "base_url": bool(settings.hermes_base_url),
                "api_key": bool(settings.hermes_api_key),
            },
        ),
    }
    alerts = _ops_alerts(integrations=integrations, settings=settings)
    return {
        "status": "degraded" if alerts else "ok",
        "environment": settings.environment,
        "integrations": integrations,
        "webhook_security": {
            "unsigned_allowed": _allow_unsigned_webhooks(settings),
            "linear": _signature_status(settings.linear_signing_secret, settings),
            "github": _signature_status(settings.github_webhook_secret, settings),
            "slack": _signature_status(settings.slack_signing_secret, settings),
            "telegram": _signature_status(settings.telegram_secret_token, settings),
        },
        "cost_observability": {
            "provider_usage_is_exact_token_source": True,
            "estimated_usage_fallback_enabled": True,
            "exact_cost_requires_provider_reported_cost": True,
            "estimated_methods": [
                "chars_per_token",
                "chars_per_token_request",
                "provider_partial",
            ],
        },
        "ops": {
            "api_auth_enabled": settings.api_auth_enabled,
            "api_auth_configured": bool(settings.api_auth_keys.strip()),
            "rate_limit_enabled": settings.api_rate_limit_enabled,
            "rate_limit_requests_per_minute": settings.api_rate_limit_requests_per_minute,
            "conversation_sync_enabled": settings.conversation_sync_enabled,
        },
        "alerts": alerts,
    }


def _integration_status(
    *,
    enabled: bool,
    required: dict[str, bool],
) -> dict[str, object]:
    missing = [name for name, configured in required.items() if not configured]
    return {
        "enabled": enabled,
        "configured": enabled and not missing,
        "ready": enabled and not missing,
        "missing": missing if enabled else [],
    }


def _ops_alerts(*, integrations: dict[str, object], settings) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    for name, status in integrations.items():
        if not isinstance(status, dict):
            continue
        if status.get("enabled") and not status.get("ready"):
            missing = ", ".join(str(item) for item in status.get("missing", []))
            alerts.append(
                {
                    "severity": "warning",
                    "code": f"{name}_not_ready",
                    "message": f"{name} is enabled but missing: {missing}",
                }
            )
    if settings.environment.lower() in {"prod", "production"} and not settings.api_auth_enabled:
        alerts.append(
            {
                "severity": "critical",
                "code": "api_auth_disabled",
                "message": "API auth should be enabled in production.",
            }
        )
    if (
        settings.environment.lower() in {"prod", "production"}
        and not settings.api_rate_limit_enabled
    ):
        alerts.append(
            {
                "severity": "warning",
                "code": "rate_limit_disabled",
                "message": "API rate limiting should be enabled in production.",
            }
        )
    if not _allow_unsigned_webhooks(settings):
        webhook_secrets = {
            "linear_webhook_secret_missing": settings.linear_signing_secret,
            "github_webhook_secret_missing": settings.github_webhook_secret,
            "slack_signing_secret_missing": settings.slack_signing_secret,
            "telegram_secret_missing": settings.telegram_secret_token,
        }
        for code, secret in webhook_secrets.items():
            if not secret:
                alerts.append(
                    {
                        "severity": "critical",
                        "code": code,
                        "message": "Public webhook endpoint requires a signing secret.",
                    }
                )
    return alerts


def _signature_status(secret: str | None, settings) -> dict[str, object]:
    unsigned_allowed = _allow_unsigned_webhooks(settings)
    return {
        "configured": bool(secret),
        "required": not unsigned_allowed,
        "ready": bool(secret) or unsigned_allowed,
    }


def _allow_unsigned_webhooks(settings) -> bool:
    return bool(settings.allow_unsigned_webhooks) or settings.environment in {
        "local",
        "dev",
        "development",
        "test",
    }
