import tomllib
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, status


@dataclass(frozen=True)
class ChannelMapping:
    provider: str
    channel: str
    repo: str
    allowed_senders: frozenset[str]


class ChannelAuthorizer:
    def __init__(self, mappings: list[ChannelMapping] | None = None) -> None:
        self._mappings = {
            (mapping.provider, mapping.channel): mapping for mapping in mappings or []
        }

    @property
    def enabled(self) -> bool:
        return bool(self._mappings)

    def authorize(self, provider: str, channel: str, sender_id: str) -> ChannelMapping | None:
        if not self.enabled:
            return None

        mapping = self._mappings.get((provider, channel))
        if mapping is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Channel is not authorized",
            )
        if mapping.allowed_senders and sender_id not in mapping.allowed_senders:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Sender is not authorized for this channel",
            )
        return mapping


def load_channel_authorizer(path: str | None) -> ChannelAuthorizer:
    if not path:
        return ChannelAuthorizer()

    with Path(path).open("rb") as file:
        document = tomllib.load(file)

    mappings = []
    for item in document.get("channels", []):
        if not isinstance(item, dict):
            continue
        provider = item.get("provider")
        channel = item.get("channel")
        repo = item.get("repo")
        allowed_senders = item.get("allowed_senders", [])
        if (
            not isinstance(provider, str)
            or not isinstance(channel, str)
            or not isinstance(repo, str)
        ):
            continue
        if not isinstance(allowed_senders, list):
            allowed_senders = []
        mappings.append(
            ChannelMapping(
                provider=provider,
                channel=channel,
                repo=repo,
                allowed_senders=frozenset(
                    sender for sender in allowed_senders if isinstance(sender, str)
                ),
            )
        )
    return ChannelAuthorizer(mappings)
