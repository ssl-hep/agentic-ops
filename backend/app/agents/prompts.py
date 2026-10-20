"""The dataset agent's system prompt, from the MLflow Prompt Registry.

The prompt text lives in MLflow (linked to the ew-kit experiment), where it is edited and versioned;
scripts/seed_prompt.py registers the first version. Each message loads the configured alias, so an
edit in MLflow applies at once, and the name/version used is logged with the message's run.
"""

import asyncio
from dataclasses import dataclass
from typing import Protocol

import mlflow
from mlflow.entities.model_registry import PromptVersion

from app.agents.llm import AgentError


@dataclass(frozen=True)
class SystemPrompt:
    text: str
    name: str
    version: str


def render(prompt: PromptVersion, **variables: str) -> SystemPrompt:
    """Fill the template's {{ variables }}; ones the template does not use are ignored."""
    used = {k: v for k, v in variables.items() if k in prompt.variables}
    return SystemPrompt(text=str(prompt.format(**used)), name=prompt.name, version=str(prompt.version))


class PromptSource(Protocol):
    async def load(self, **variables: str) -> SystemPrompt: ...


class MlflowPrompt:
    """One prompt from the registry, e.g. 'ew-kit-dataset-narrower@champion', fetched at every call."""

    def __init__(self, uri: str, configured: bool):
        self.uri = uri if uri.startswith("prompts:/") else f"prompts:/{uri}"
        self.configured = configured

    async def load(self, **variables: str) -> SystemPrompt:
        if not self.configured:
            raise AgentError(f"No MLflow tracking server is configured (MLFLOW_TRACKING_URI); {self.uri} lives there.")
        try:
            # Blocking HTTP, so off the event loop
            prompt = await asyncio.to_thread(mlflow.genai.load_prompt, self.uri, link_to_model=False, cache_ttl_seconds=0)
        except Exception as exc:
            raise AgentError(f"Could not load {self.uri} from MLflow: {type(exc).__name__}: {exc}") from exc
        return render(prompt, **variables)
