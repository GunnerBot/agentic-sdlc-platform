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
    api_auth_enabled: bool = False
    api_auth_keys: str = Field(default="", repr=False)
    allow_unsigned_webhooks: bool = False
    api_auth_exempt_path_prefixes: str = (
        "/healthz,/readyz,/openapi.json,/docs,/redoc,/webhooks,"
        "/channels/slack,/channels/telegram"
    )
    api_rate_limit_enabled: bool = False
    api_rate_limit_requests_per_minute: int = 120
    api_rate_limit_exempt_path_prefixes: str = "/healthz,/readyz"

    linear_signing_secret: str | None = None
    linear_http_enabled: bool = False
    linear_base_url: str = "https://api.linear.app/graphql"
    linear_api_key: str | None = Field(default=None, repr=False)
    linear_timeout_seconds: float = 10.0
    linear_agent_user_id: str | None = None
    linear_team_id: str | None = None
    linear_spec_planner_enabled: bool = False
    linear_plan_approval_required: bool = False
    github_webhook_secret: str | None = None
    github_app_read_only_enabled: bool = False
    github_app_slug: str | None = None
    github_app_write_enabled_default: bool = True
    github_app_id: str | None = None
    github_app_installation_id: str | None = None
    github_app_private_key: str | None = Field(default=None, repr=False)
    github_app_private_key_path: str | None = Field(default=None, repr=False)
    github_app_api_base_url: str = "https://api.github.com"
    github_app_timeout_seconds: float = 10.0
    github_app_git_credential_enabled: bool = False
    github_app_git_credential_hosts: str = "github.com"
    github_app_git_credential_allowed_owners: str = ""
    slack_signing_secret: str | None = Field(default=None, repr=False)
    slack_bot_token: str | None = Field(default=None, repr=False)
    slack_signature_tolerance_seconds: int = 300
    slack_api_base_url: str = "https://slack.com/api"
    slack_timeout_seconds: float = 10.0
    telegram_secret_token: str | None = Field(default=None, repr=False)
    telegram_bot_token: str | None = Field(default=None, repr=False)
    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_timeout_seconds: float = 10.0
    channel_mapping_path: str | None = None
    channel_cost_cap_usd: float | None = None
    channel_default_request_cost_usd: float = 0.01
    observability_chars_per_token: float = 4.0
    observability_input_cost_per_million_usd: float = 0.25
    observability_output_cost_per_million_usd: float = 2.00
    multica_http_enabled: bool = False
    multica_base_url: str | None = None
    multica_api_key: str | None = Field(default=None, repr=False)
    multica_workspace_id: str | None = None
    multica_default_runtime_provider: str = "codex"
    multica_agent_name_prefix: str = "agentic-sdlc"
    multica_timeout_seconds: float = 10.0
    multica_max_retries: int = 2
    multica_retry_backoff_seconds: float = 0.25
    database_url: str = "postgresql+asyncpg://agentic_sdlc:agentic_sdlc@localhost:5432/agentic_sdlc"

    vendor_http_enabled: bool = False
    model_provider: str = "openai"
    claude_base_url: str = "https://api.anthropic.com"
    claude_api_key: str | None = Field(default=None, repr=False)
    claude_default_model: str | None = None
    claude_timeout_seconds: float = 30.0
    claude_max_retries: int = 2
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str | None = Field(default=None, repr=False)
    openai_default_model: str = "gpt-5-mini"
    openai_fallback_model: str = "gpt-5"
    openai_router_model: str = "gpt-5-nano"
    openai_summary_model: str = "gpt-5-nano"
    openai_qa_model: str = "gpt-5-mini"
    openai_planner_model: str = "gpt-5-mini"
    openai_planner_escalation_model: str = "gpt-5"
    openai_write_model: str = "gpt-5-mini"
    openai_write_escalation_model: str = "gpt-5"
    openai_premium_escalation_model: str = "gpt-5.5"
    openai_timeout_seconds: float = 30.0
    openai_max_retries: int = 1

    graphify_base_url: str | None = None
    graphify_api_key: str | None = Field(default=None, repr=False)
    graphify_mode: str = "cli"
    graphify_command: str = "graphify"
    graphify_output_root: str | None = None
    graphify_timeout_seconds: float = 10.0
    graphify_max_retries: int = 2
    graphify_context_max_chars: int = 4000
    graphify_context_max_references: int = 10
    repo_cache_root: str | None = None
    repo_cache_clone_timeout_seconds: float = 300.0

    notion_http_enabled: bool = False
    notion_base_url: str = "https://api.notion.com"
    notion_api_key: str | None = Field(default=None, repr=False)
    notion_version: str = "2022-06-28"
    notion_timeout_seconds: float = 10.0
    google_docs_http_enabled: bool = False
    google_docs_base_url: str = "https://docs.google.com"
    google_docs_bearer_token: str | None = Field(default=None, repr=False)
    google_docs_timeout_seconds: float = 10.0
    figma_http_enabled: bool = False
    figma_base_url: str = "https://api.figma.com"
    figma_api_key: str | None = Field(default=None, repr=False)
    figma_timeout_seconds: float = 10.0
    design_image_hydration_enabled: bool = False
    design_image_summary_provider: str = "openai"
    design_image_summary_model: str | None = "gpt-5-nano"
    design_image_max_bytes: int = 5_000_000
    design_image_fetch_timeout_seconds: float = 10.0

    hermes_http_enabled: bool = False
    hermes_api_mode: str = "native"
    hermes_base_url: str | None = None
    hermes_api_key: str | None = Field(default=None, repr=False)
    hermes_model: str = "hermes-agent"
    hermes_timeout_seconds: float = 10.0
    hermes_max_retries: int = 1

    agent_default_execution_mode: str = "dry_run"
    agent_readonly_max_model_retries: int = 0
    agent_write_max_model_retries: int = 1
    adversarial_review_loop_enabled: bool = False
    adversarial_review_max_turns: int = 3
    adversarial_review_model: str | None = None

    agent_executor_enabled: bool = False
    agent_executor_provider: str = "local"
    agent_executor_workspace_root: str = "/tmp/agentic-sdlc-platform/workspaces"

    conversation_sync_enabled: bool = False
    conversation_sync_interval_seconds: float = 15.0
    conversation_sync_batch_size: int = 50


@lru_cache
def get_settings() -> Settings:
    return Settings()
