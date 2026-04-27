from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ASDLC_",
        env_file=(".env", ".env.local"),
        extra="ignore",
    )

    service_name: str = "agentic-sdlc-platform"
    version: str = "0.1.0"
    environment: str = "local"
    host: str = "0.0.0.0"
    port: int = 8080
    reload: bool = False
    docs_enabled: bool = True

    linear_signing_secret: str | None = None
    linear_http_enabled: bool = False
    linear_base_url: str = "https://api.linear.app/graphql"
    linear_api_key: str | None = Field(default=None, repr=False)
    linear_timeout_seconds: float = 10.0
    linear_agent_user_id: str | None = None
    linear_team_id: str | None = None
    github_webhook_secret: str | None = None
    github_app_read_only_enabled: bool = False
    github_app_id: str | None = None
    github_app_installation_id: str | None = None
    github_app_private_key: str | None = Field(default=None, repr=False)
    github_app_private_key_path: str | None = Field(default=None, repr=False)
    github_app_api_base_url: str = "https://api.github.com"
    github_app_timeout_seconds: float = 10.0
    slack_signing_secret: str | None = Field(default=None, repr=False)
    slack_bot_token: str | None = Field(default=None, repr=False)
    slack_signature_tolerance_seconds: int = 300
    slack_api_base_url: str = "https://slack.com/api"
    slack_timeout_seconds: float = 10.0
    telegram_secret_token: str | None = Field(default=None, repr=False)
    channel_mapping_path: str | None = None
    channel_cost_cap_usd: float | None = None
    channel_default_request_cost_usd: float = 0.01
    multica_http_enabled: bool = False
    multica_base_url: str | None = None
    multica_api_key: str | None = Field(default=None, repr=False)
    multica_timeout_seconds: float = 10.0
    multica_max_retries: int = 2
    multica_retry_backoff_seconds: float = 0.25
    database_url: str = "postgresql+asyncpg://agentic_sdlc:agentic_sdlc@localhost:5432/agentic_sdlc"

    vendor_http_enabled: bool = False
    claude_base_url: str = "https://api.anthropic.com"
    claude_api_key: str | None = Field(default=None, repr=False)
    claude_default_model: str | None = None
    claude_timeout_seconds: float = 30.0
    claude_max_retries: int = 2

    graphify_base_url: str | None = None
    graphify_api_key: str | None = Field(default=None, repr=False)
    graphify_timeout_seconds: float = 10.0
    graphify_max_retries: int = 2

    hermes_http_enabled: bool = False
    hermes_base_url: str | None = None
    hermes_api_key: str | None = Field(default=None, repr=False)
    hermes_timeout_seconds: float = 10.0
    hermes_max_retries: int = 2

    agent_executor_enabled: bool = False
    agent_executor_provider: str = "local"
    agent_executor_workspace_root: str = "/tmp/agentic-sdlc-platform/workspaces"


@lru_cache
def get_settings() -> Settings:
    return Settings()
