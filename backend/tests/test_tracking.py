"""MLflow wiring: credentials kept out of the URI, and prompts rendered from the registry's templates."""

import asyncio

import pytest
from conftest import settings
from mlflow.entities.model_registry import PromptVersion

from app.agents import prompts
from app.agents.llm import AgentError
from app.agents.prompts import MlflowPrompts, render
from app.agents.seed_prompts import TEMPLATES
from app.tracking import split_credentials, tracking_uri
from app.validator import MEMORY_RANGE_MB


def test_split_credentials() -> None:
    assert split_credentials("https://alice:s3cret@mlflow.example.org:8443/x") == (
        "https://mlflow.example.org:8443/x",
        "alice",
        "s3cret",
    )
    assert split_credentials("https://mlflow.example.org") == ("https://mlflow.example.org", None, None)


def test_tracking_uri_never_shows_credentials() -> None:
    assert tracking_uri(settings(mlflow_tracking_uri="https://alice:s3cret@mlflow.example.org")) == "https://mlflow.example.org"
    assert tracking_uri(settings()) is None


@pytest.mark.parametrize("name", [prompts.BUILDER, prompts.DATASETS, prompts.REVIEWER])
def test_seed_templates_render(name: str) -> None:
    system = render(PromptVersion(name, 3, TEMPLATES[name][0]))
    assert system.version == f"{name}/3"
    assert "# EWKit knowledge card" in system.text
    assert "{{" not in system.text
    if name != prompts.DATASETS:
        assert f"{MEMORY_RANGE_MB[0]}–{MEMORY_RANGE_MB[1]} MB" in system.text


def test_prompts_need_mlflow() -> None:
    with pytest.raises(AgentError, match="MLFLOW_TRACKING_URI"):
        asyncio.run(MlflowPrompts(configured=False).load(prompts.BUILDER))
