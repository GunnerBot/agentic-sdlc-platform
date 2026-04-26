from dataclasses import dataclass
from typing import Protocol


class HermesSessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class HermesSessionRequest:
    provider: str
    channel: str
    sender_id: str
    text: str
    repo: str | None = None


@dataclass(frozen=True)
class HermesSessionResponse:
    session_id: str
    message_id: str
    answer: str | None = None


class HermesSessionPort(Protocol):
    async def ask(self, request: HermesSessionRequest) -> HermesSessionResponse:
        raise NotImplementedError
