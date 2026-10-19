"""System prompts for the run setup agents, from the MLflow Prompt Registry.

The prompt texts live in MLflow (linked to the ew-kit experiment), where they are edited and
versioned. Each agent call loads the latest version, and records its name/version with the draft or
review it made. Two parts stay in code and fill the templates' variables: {{ knowledge }} (the
knowledge card) and {{ run_policy }}, which must match the validator. seed_prompts.py registers the
first versions on a new tracking server.
"""

import asyncio
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Protocol

import mlflow
from mlflow.entities.model_registry import PromptVersion

from app.agents.llm import AgentError
from app.validator import MEMORY_RANGE_MB

BUILDER = "ew-kit-builder"
DATASETS = "ew-kit-datasets"
REVIEWER = "ew-kit-reviewer"


@cache
def knowledge_card() -> str:
    return (Path(__file__).parent / "knowledge.md").read_text()


def run_policy() -> str:
    return f"""# Run policy (from the app's settings)

- purpose 'production' needs a git tag; branches and commits are allowed only with purpose 'test' (D4).
- memory_mb outside {MEMORY_RANGE_MB[0]}–{MEMORY_RANGE_MB[1]} MB is flagged.
- No config or sample list is ever edited by this app. Problems in them go to the folder's code owners.
"""


@dataclass(frozen=True)
class SystemPrompt:
    text: str
    # "<name>/<version>" in the Prompt Registry
    version: str


def render(prompt: PromptVersion) -> SystemPrompt:
    text = prompt.format(knowledge=knowledge_card(), run_policy=run_policy())
    return SystemPrompt(text=str(text), version=f"{prompt.name}/{prompt.version}")


class PromptSource(Protocol):
    async def load(self, name: str) -> SystemPrompt: ...


class MlflowPrompts:
    """The latest version of each prompt, fetched at every call so an edit in MLflow applies at once."""

    def __init__(self, configured: bool):
        self.configured = configured

    async def load(self, name: str) -> SystemPrompt:
        if not self.configured:
            raise AgentError(f"No MLflow tracking server is configured (MLFLOW_TRACKING_URI); the {name} prompt lives there.")
        try:
            # Blocking HTTP, so off the event loop. Loaded inside a trace, the version is linked to it.
            prompt = await asyncio.to_thread(
                mlflow.genai.load_prompt, f"prompts:/{name}@latest", link_to_model=False, cache_ttl_seconds=0
            )
            return render(prompt)
        except Exception as exc:
            raise AgentError(f"Could not load the {name} prompt from MLflow: {type(exc).__name__}: {exc}") from exc
