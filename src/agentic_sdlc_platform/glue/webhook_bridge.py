import hmac
import json
from dataclasses import dataclass
from hashlib import sha256

from fastapi import HTTPException, status

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.glue.task_event_normalizer import TaskEventNormalizer
from agentic_sdlc_platform.models.webhooks import WebhookAcceptedResponse
from agentic_sdlc_platform.persistence.repository import (
    InboundEventWriteResult,
    PersistenceRepository,
)
from agentic_sdlc_platform.ports.task_orchestrator import TaskOrchestratorPort, TaskRequest


@dataclass(frozen=True)
class RecordedDelivery:
    inbound_event: InboundEventWriteResult
    task_id: str | None = None


class WebhookBridge:
    def __init__(
        self,
        settings: Settings,
        repository: PersistenceRepository,
        task_orchestrator: TaskOrchestratorPort | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._task_orchestrator = task_orchestrator
        self._normalizer = TaskEventNormalizer()

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
            task_id=result.task_id,
            delivery_id=delivery_id,
            duplicate=not result.inbound_event.created,
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
            task_id=result.task_id,
            delivery_id=delivery_id,
            duplicate=not result.inbound_event.created,
        )

    async def _record_delivery(
        self,
        source: str,
        delivery_id: str,
        event_type: str,
        payload: bytes,
    ) -> RecordedDelivery:
        parsed_payload = self._parse_payload(payload)
        result = await self._repository.record_inbound_event(
            source=source,
            delivery_id=delivery_id,
            event_type=event_type,
            payload=parsed_payload,
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
        task_id = await self._normalize_task(result, source, event_type, parsed_payload)
        return RecordedDelivery(inbound_event=result, task_id=task_id)

    async def _normalize_task(
        self,
        result: InboundEventWriteResult,
        source: str,
        event_type: str,
        payload: dict[str, object],
    ) -> str | None:
        if not result.created:
            return None

        task_event = self._normalizer.normalize(
            source=source,
            event_type=event_type,
            payload=payload,
        )
        if task_event is None:
            return None

        task = await self._repository.create_task_from_event(
            event_id=result.event.id,
            source=task_event.source,
            external_id=task_event.external_id,
            title=task_event.title,
            repo=task_event.repo,
        )
        await self._repository.record_audit_event(
            action="task.normalized",
            actor="system",
            target_type="task",
            target_id=task.id,
            metadata={
                "source": task_event.source,
                "external_id": task_event.external_id,
                "repo": task_event.repo,
            },
        )
        if self._task_orchestrator is not None:
            external_task = await self._task_orchestrator.create_task(
                TaskRequest(
                    source=task_event.source,
                    external_id=task_event.external_id,
                    title=task_event.title,
                    repo=task_event.repo,
                    inbound_event_id=result.event.id,
                )
            )
            task = await self._repository.mark_task_orchestrated(
                task_id=task.id,
                orchestrator_task_id=external_task.external_task_id,
                orchestrator_status=external_task.status,
            )
            await self._repository.record_audit_event(
                action="task.orchestrated",
                actor="system",
                target_type="task",
                target_id=task.id,
                metadata={
                    "provider": self._task_orchestrator.provider,
                    "external_task_id": external_task.external_task_id,
                    "status": external_task.status,
                },
            )
        return task.id

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
