from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read from the environment, or for local development from backend/.env, then the repo-root .env."""

    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    gitlab_url: str = "https://gitlab.cern.ch"
    gitlab_project: str = "atlas-physics/hmbs/ewk/EWKit"
    # Read-only token (read_api). Never logged, never sent to the browser.
    gitlab_token: SecretStr | None = None
    gitlab_timeout_seconds: float = 30.0
    gitlab_max_concurrency: int = 8

    # OpenAI-compatible chat completions endpoint for the dataset agent, e.g. https://llm.example.org/v1.
    # The agent is off unless both the base URL and a model are set.
    llm_base_url: str | None = None
    # Sent as a bearer token. Never logged, never sent to the browser or the model.
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_temperature: float = 0.0
    llm_timeout_seconds: float = 120.0
    # Tool calls (dataset and Rucio) the agent may make per message before it must answer
    llm_max_tool_calls: int = 12

    # MLflow tracking server: the agent's system prompt comes from its Prompt Registry, and every
    # message is logged as a run in the experiment. Basic-auth credentials can be embedded
    # (https://user:pass@host) or given as the username and password.
    mlflow_tracking_uri: SecretStr | None = None
    mlflow_tracking_username: str | None = None
    mlflow_tracking_password: SecretStr | None = None
    mlflow_experiment: str = "ew-kit"
    # Prompt name and alias (or version) in the Prompt Registry
    mlflow_prompt: str = "ew-kit-dataset-narrower@champion"

    # AF MCP gateway, for read-only Rucio lookups. The token is a shared service token.
    mcp_url: str | None = "https://mcp.af.uchicago.edu/mcp/"
    mcp_bearer_token: SecretStr | None = None
    # Globs of the MCP tools the agent may call; tools not marked read-only are dropped regardless
    mcp_tool_allowlist: list[str] = ["rucio_list*", "rucio_get*"]

    # Where the agent's dataset selections are written, as <scratch_dir>/<folder>/<id>.txt
    scratch_dir: Path = Path("scratch")

    @property
    def agent_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)


@lru_cache
def get_settings() -> Settings:
    return Settings()
