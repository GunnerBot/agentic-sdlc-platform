from dataclasses import dataclass
from typing import Protocol


class HermesSessionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        usage: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = usage


@dataclass(frozen=True)
class HermesSessionRequest:
    provider: str
    channel: str
    sender_id: str
    text: str
    repo: str | None = None


@dataclass(frozen=True)
class HermesStartSessionRequest:
    task_id: str
    provider: str
    external_thread_id: str
    text: str
    repo: str | None = None


@dataclass(frozen=True)
class HermesSessionResponse:
    session_id: str
    message_id: str
    answer: str | None = None
    usage: dict[str, object] | None = None


class HermesSessionPort(Protocol):
    async def ask(self, request: HermesSessionRequest) -> HermesSessionResponse:
        raise NotImplementedError

    async def start_session(self, request: HermesStartSessionRequest) -> HermesSessionResponse:
        raise NotImplementedError

    async def resume_session(
        self,
        session_id: str,
        text: str,
        actor: str,
    ) -> HermesSessionResponse:
        raise NotImplementedError
