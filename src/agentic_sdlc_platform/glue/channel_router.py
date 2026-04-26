from dataclasses import dataclass
from enum import StrEnum


class RouteTarget(StrEnum):
    HERMES_DIRECT = "hermes_direct"
    MULTICA_TASK = "multica_task"


@dataclass(frozen=True)
class ChannelMessage:
    channel: str
    text: str
    sender_id: str


class ChannelRouter:
    def route(self, message: ChannelMessage) -> RouteTarget:
        normalized = message.text.strip().lower()
        if normalized.startswith(("/implement", "/ticket", "linear:")):
            return RouteTarget.MULTICA_TASK
        return RouteTarget.HERMES_DIRECT
