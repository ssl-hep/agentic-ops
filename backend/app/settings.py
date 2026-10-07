from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read from the environment, or from backend/.env for local development."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gitlab_url: str = "https://gitlab.cern.ch"
    gitlab_project: str = "atlas-physics/hmbs/ewk/EWKit"
    # Read-only token (read_api). Never logged, never sent to the browser.
    gitlab_token: SecretStr | None = None
    gitlab_timeout_seconds: float = 30.0
    gitlab_max_concurrency: int = 8


@lru_cache
def get_settings() -> Settings:
    return Settings()
