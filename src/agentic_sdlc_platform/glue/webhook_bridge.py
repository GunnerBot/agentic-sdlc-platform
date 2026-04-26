import hmac
import json
from hashlib import sha256

from fastapi import HTTPException, status

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.models.webhooks import WebhookAcceptedResponse
from agentic_sdlc_platform.persistence.repository import (
    InboundEventWriteResult,
    PersistenceRepository,
)


class WebhookBridge:
    def __init__(self, settings: Settings, repository: PersistenceRepository) -> None:
        self._settings = settings
        self._repository = repository

    async def accept_linear(
        self,
        payload: bytes,
        delivery_id: str,
        signature: str | None,
    ) -> WebhookAcceptedResponse:
        self._verify_optional_hmac(
            payload=payload,
            signature=signature,
            secret=self._settings.linear_signing_secret,
            prefix=None,
        )
        result = await self._record_delivery(
            source="linear",
            delivery_id=delivery_id,
            event_type=self._extract_event_type(payload, default="unknown"),
            payload=payload,
        )
        return WebhookAcceptedResponse(
            accepted=True,
            source="linear",
            delivery_id=delivery_id,
            duplicate=not result.created,
        )

    async def accept_github(
        self,
        payload: bytes,
        event: str | None,
        delivery_id: str,
        signature: str | None,
    ) -> WebhookAcceptedResponse:
        if not event:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing X-GitHub-Event header",
            )

        self._verify_optional_hmac(
            payload=payload,
            signature=signature,
            secret=self._settings.github_webhook_secret,
            prefix="sha256=",
        )
        result = await self._record_delivery(
            source="github",
            delivery_id=delivery_id,
            event_type=event,
            payload=payload,
        )
        return WebhookAcceptedResponse(
            accepted=True,
            source=f"github:{event}",
            delivery_id=delivery_id,
            duplicate=not result.created,
        )

    async def _record_delivery(
        self,
        source: str,
        delivery_id: str,
        event_type: str,
        payload: bytes,
    ) -> InboundEventWriteResult:
        result = await self._repository.record_inbound_event(
            source=source,
            delivery_id=delivery_id,
            event_type=event_type,
            payload=self._parse_payload(payload),
        )
        await self._repository.record_audit_event(
            action="webhook.accepted" if result.created else "webhook.duplicate",
            actor="system",
            target_type="inbound_event",
            target_id=result.event.id,
            metadata={
                "source": source,
                "delivery_id": delivery_id,
                "event_type": event_type,
            },
        )
        return result

    def _verify_optional_hmac(
        self,
        payload: bytes,
        signature: str | None,
        secret: str | None,
        prefix: str | None,
    ) -> None:
        if not secret:
            return

        if not signature:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing webhook signature",
            )

        digest = hmac.new(secret.encode("utf-8"), payload, sha256).hexdigest()
        expected = f"{prefix or ''}{digest}"
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid webhook signature",
            )

    def _parse_payload(self, payload: bytes) -> dict[str, object]:
        if not payload:
            return {}
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError:
            return {"raw": payload.decode("utf-8", errors="replace")}
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    def _extract_event_type(self, payload: bytes, default: str) -> str:
        parsed = self._parse_payload(payload)
        event_type = parsed.get("type")
        return event_type if isinstance(event_type, str) else default
