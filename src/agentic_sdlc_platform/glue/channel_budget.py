from dataclasses import dataclass, field

from fastapi import HTTPException, status


@dataclass
class ChannelBudgetLedger:
    cap_usd: float | None
    default_request_cost_usd: float
    _spend_by_channel: dict[tuple[str, str], float] = field(default_factory=dict)

    def reserve(self, provider: str, channel: str) -> None:
        if self.cap_usd is None:
            return

        key = (provider, channel)
        current_spend = self._spend_by_channel.get(key, 0.0)
        next_spend = current_spend + self.default_request_cost_usd
        if next_spend > self.cap_usd:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Channel cost cap exceeded",
            )
        self._spend_by_channel[key] = next_spend
