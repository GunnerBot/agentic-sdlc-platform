from pydantic import BaseModel


class WebhookAcceptedResponse(BaseModel):
    accepted: bool
    source: str
    task_id: str | None = None
