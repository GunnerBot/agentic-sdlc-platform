from dataclasses import dataclass

from agentic_sdlc_platform.core.config import Settings


@dataclass(frozen=True)
class ModelRoute:
    provider: str
    model: str


class CostRouter:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()

    def route(self, role: str) -> ModelRoute:
        normalized = role.strip().lower().replace("-", "_").replace(" ", "_")
        defaults = {
            "router_agent": ModelRoute(
                provider="openai",
                model=self._settings.openai_router_model,
            ),
            "intent_router": ModelRoute(
                provider="openai",
                model=self._settings.openai_router_model,
            ),
            "summary_agent": ModelRoute(
                provider="openai",
                model=self._settings.openai_summary_model,
            ),
            "summarizer_agent": ModelRoute(
                provider="openai",
                model=self._settings.openai_summary_model,
            ),
            "qa_agent": ModelRoute(provider="openai", model=self._settings.openai_qa_model),
            "question_answering_agent": ModelRoute(
                provider="openai",
                model=self._settings.openai_qa_model,
            ),
            "plan_agent": ModelRoute(
                provider="openai",
                model=self._settings.openai_planner_model,
            ),
            "planner_agent": ModelRoute(
                provider="openai",
                model=self._settings.openai_planner_model,
            ),
            "planner_escalation_agent": ModelRoute(
                provider="openai",
                model=self._settings.openai_planner_escalation_model,
            ),
            "impl_agent": ModelRoute(provider="openai", model=self._settings.openai_write_model),
            "implementation_agent": ModelRoute(
                provider="openai",
                model=self._settings.openai_write_model,
            ),
            "write_agent": ModelRoute(
                provider="openai",
                model=self._settings.openai_write_model,
            ),
            "critic_agent": ModelRoute(
                provider="openai",
                model=self._settings.openai_write_escalation_model,
            ),
            "review_agent": ModelRoute(
                provider="openai",
                model=self._settings.openai_write_escalation_model,
            ),
            "write_escalation_agent": ModelRoute(
                provider="openai",
                model=self._settings.openai_write_escalation_model,
            ),
            "premium_escalation_agent": ModelRoute(
                provider="openai",
                model=self._settings.openai_premium_escalation_model,
            ),
            "most_complex_agent": ModelRoute(
                provider="openai",
                model=self._settings.openai_premium_escalation_model,
            ),
        }
        return defaults.get(
            normalized,
            ModelRoute(provider="openai", model=self._settings.openai_default_model),
        )
