from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ASDLC_", env_file=".env", extra="ignore")

    service_name: str = "agentic-sdlc-platform"
    version: str = "0.1.0"
    environment: str = "local"
    host: str = "0.0.0.0"
    port: int = 8080
    reload: bool = False
    docs_enabled: bool = True

    linear_signing_secret: str | None = None
    github_webhook_secret: str | None = None
    multica_base_url: str | None = None
    multica_api_key: str | None = Field(default=None, repr=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
