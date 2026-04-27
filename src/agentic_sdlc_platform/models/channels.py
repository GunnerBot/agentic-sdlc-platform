from enum import StrEnum

from pydantic import BaseModel, Field


class ChannelProvider(StrEnum):
    SLACK = "slack"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    CLI = "cli"


class ChannelMessageRequest(BaseModel):
    provider: ChannelProvider
    channel: str = Field(min_length=1)
    sender_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    repo: str | None = Field(default=None, min_length=1)
    thread_id: str | None = Field(default=None, min_length=1)


class ChannelAcceptedResponse(BaseModel):
    accepted: bool
    provider: ChannelProvider
    channel: str
    route: str
    session_id: str | None = None
    message_id: str | None = None
    task_id: str | None = None
    command: str | None = None
    repo: str | None = None
    answer: str | None = None
    references: list[str] | None = None
    issue_id: str | None = None
    external_id: str | None = None
    url: str | None = None
