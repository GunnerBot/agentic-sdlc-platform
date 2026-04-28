from dataclasses import dataclass


@dataclass(frozen=True)
class ModelRoute:
    provider: str
    model: str


class CostRouter:
    def route(self, role: str) -> ModelRoute:
        defaults = {
            "critic_agent": ModelRoute(provider="openai", model="gpt-5.4-mini"),
            "review_agent": ModelRoute(provider="openai", model="gpt-5.4-mini"),
            "impl_agent": ModelRoute(provider="openrouter", model="moonshotai/kimi-k2.6"),
        }
        return defaults.get(role, ModelRoute(provider="zai", model="glm-5.1"))
