import httpx

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.glue.llm_observability import (
    estimated_llm_usage,
    usage_from_openai_payload,
)
from agentic_sdlc_platform.ports.hermes_session import (
    HermesSessionError,
    HermesSessionRequest,
    HermesSessionResponse,
    HermesStartSessionRequest,
)


class HermesAgentAdapter:
    provider = "hermes"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def ask(self, request: HermesSessionRequest) -> HermesSessionResponse:
        if self._settings.hermes_api_mode == "openai_compatible":
            prompt = "\n\n".join(
                [
                    f"Channel provider: {request.provider}",
                    f"Channel: {request.channel}",
                    f"Sender: {request.sender_id}",
                    f"Repo: {request.repo or 'none'}",
                    request.text,
                ]
            )
            return await self._responses_create(
                operation="hermes.ask",
                input_text=prompt,
                instructions=_agent_instructions(),
                failure_message="hermes ask failed",
            )

        payload = {
            "provider": request.provider,
            "channel": request.channel,
            "sender_id": request.sender_id,
            "text": request.text,
            "repo": request.repo,
            **_agent_policy_payload(),
        }
        response = await self._post(
            path="/api/sessions/ask",
            payload=payload,
            failure_message="hermes ask failed",
        )
        return self._session_response(
            response,
            failure_message="hermes ask returned invalid response",
        )

    async def start_session(self, request: HermesStartSessionRequest) -> HermesSessionResponse:
        if self._settings.hermes_api_mode == "openai_compatible":
            prompt = "\n\n".join(
                [
                    f"Task id: {request.task_id}",
                    f"Provider: {request.provider}",
                    f"External thread id: {request.external_thread_id}",
                    f"Repo: {request.repo or 'none'}",
                    request.text,
                ]
            )
            return await self._responses_create(
                operation="hermes.start_session",
                input_text=prompt,
                instructions=_agent_instructions(),
                conversation=f"{request.provider}:{request.external_thread_id}",
                failure_message="hermes start_session failed",
            )

        payload = {
            "task_id": request.task_id,
            "provider": request.provider,
            "external_thread_id": request.external_thread_id,
            "text": request.text,
            "repo": request.repo,
            **_agent_policy_payload(),
        }
        response = await self._post(
            path="/api/sessions",
            payload=payload,
            failure_message="hermes start_session failed",
        )
        return self._session_response(
            response,
            failure_message="hermes start_session returned invalid response",
        )

    async def resume_session(
        self,
        session_id: str,
        text: str,
        actor: str,
    ) -> HermesSessionResponse:
        if self._settings.hermes_api_mode == "openai_compatible":
            return await self._responses_create(
                operation="hermes.resume_session",
                input_text=f"{actor}: {text}",
                previous_response_id=session_id,
                instructions=_agent_instructions(),
                failure_message="hermes resume_session failed",
            )

        response = await self._post(
            path=f"/api/sessions/{session_id}/messages",
            payload={"text": text, "actor": actor, **_agent_policy_payload()},
            failure_message="hermes resume_session failed",
        )
        return self._session_response(
            response,
            failure_message="hermes resume_session returned invalid response",
        )

    async def _post(
        self,
        path: str,
        payload: dict[str, object | None],
        failure_message: str,
    ) -> dict[str, object]:
        if not self._settings.hermes_http_enabled:
            raise HermesSessionError("hermes HTTP is disabled")
        if not self._settings.hermes_base_url:
            raise HermesSessionError("hermes base URL is not configured")
        if not self._settings.hermes_api_key:
            raise HermesSessionError("hermes API key is not configured")

        try:
            async with httpx.AsyncClient(
                base_url=self._settings.hermes_base_url,
                timeout=self._settings.hermes_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    path,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._settings.hermes_api_key}"},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HermesSessionError(failure_message) from exc

        return response.json()

    async def _responses_create(
        self,
        *,
        operation: str,
        input_text: str,
        instructions: str,
        failure_message: str,
        previous_response_id: str | None = None,
        conversation: str | None = None,
    ) -> HermesSessionResponse:
        payload: dict[str, object] = {
            "model": self._settings.hermes_model,
            "input": input_text,
            "instructions": instructions,
            "store": True,
        }
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id
        if conversation:
            payload["conversation"] = conversation
        request_input_text = f"{instructions}\n\n{input_text}"
        failure_usage = estimated_llm_usage(
            settings=self._settings,
            model=self._settings.hermes_model,
            operation=operation,
            input_text=request_input_text,
            estimation_method="chars_per_token_request",
        )
        try:
            response = await self._post(
                path="/v1/responses",
                payload=payload,
                failure_message=failure_message,
            )
        except HermesSessionError as exc:
            raise HermesSessionError(
                str(exc),
                usage={
                    **failure_usage,
                    "failed": True,
                },
            ) from exc
        response_id = response.get("id")
        if not isinstance(response_id, str):
            raise HermesSessionError(f"{failure_message}: response id missing")
        answer = _extract_responses_text(response)
        return HermesSessionResponse(
            session_id=response_id,
            message_id=response_id,
            answer=answer,
            usage=usage_from_openai_payload(
                payload=response,
                settings=self._settings,
                model=self._settings.hermes_model,
                operation=operation,
                request_input_text=request_input_text,
                response_output_text=answer,
            ),
        )

    def _session_response(
        self,
        response_payload: dict[str, object],
        failure_message: str,
    ) -> HermesSessionResponse:
        session_id = response_payload.get("session_id")
        message_id = response_payload.get("message_id")
        answer = response_payload.get("answer")
        if not isinstance(session_id, str) or not isinstance(message_id, str):
            raise HermesSessionError(failure_message)

        return HermesSessionResponse(
            session_id=session_id,
            message_id=message_id,
            answer=answer if isinstance(answer, str) else None,
        )


def _agent_policy_payload() -> dict[str, object]:
    return {
        "runtime_policy": {
            "shell_command_prefix": "rtk",
            "use_rtk_for_terminal_commands": True,
        },
        "repo_context_policy": {
            "preferred_context_source": "graphify",
            "verify_graph_context_against_source": True,
            "avoid_repeated_broad_scans_when_indexed_context_is_available": True,
        },
    }


def _agent_instructions() -> str:
    return (
        "You are Hermes running as the direct agent backend for the Agentic SDLC Platform. "
        "Use rtk for terminal commands. Prefer Graphify/repository context when supplied, "
        "then verify narrowly against source. Do not create GitHub branches or PRs unless "
        "the caller explicitly provides write-enabled credentials and instructions."
    )


def _extract_responses_text(payload: dict[str, object]) -> str | None:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text
    output = payload.get("output")
    if not isinstance(output, list):
        return None
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for content_item in content:
            if isinstance(content_item, dict):
                text = content_item.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts) if parts else None
