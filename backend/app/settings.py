from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

StructuredOutputMethod = Literal["function_calling", "json_schema", "json_mode"]


class Settings(BaseSettings):
    """Read from the environment, or from backend/.env for local development."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gitlab_url: str = "https://gitlab.cern.ch"
    gitlab_project: str = "atlas-physics/hmbs/ewk/EWKit"
    # Read-only token (read_api). Never logged, never sent to the browser.
    gitlab_token: SecretStr | None = None
    gitlab_timeout_seconds: float = 30.0
    gitlab_max_concurrency: int = 8

    # OpenAI-compatible model endpoint for the run setup agents, e.g. https://llm.example.org/v1.
    # The agents are off unless both the base URL and a model are set.
    llm_base_url: str | None = None
    # Sent as a bearer token. Never logged, never sent to the browser or the model.
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    # Optional per-agent overrides of llm_model
    llm_builder_model: str | None = None
    llm_reviewer_model: str | None = None
    llm_temperature: float = 0.0
    llm_timeout_seconds: float = 120.0
    # Repository tool calls each agent may make before it must answer
    llm_max_tool_calls: int = 8
    # function_calling works with most OpenAI-compatible servers (vLLM, LiteLLM, Ollama);
    # json_schema needs OpenAI-style structured outputs
    llm_structured_output: StructuredOutputMethod = "function_calling"

    # Postgres for LangGraph checkpoints; without it, run setups live in memory only
    database_url: SecretStr | None = None

    @property
    def agents_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)


@lru_cache
def get_settings() -> Settings:
    return Settings()
