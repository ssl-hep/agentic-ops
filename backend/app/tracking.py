"""MLflow: the dataset agent's prompt comes from its Prompt Registry, and every message is logged there.

Each message to the agent becomes one MLflow run in the experiment (MLFLOW_EXPERIMENT), holding the
message, the reply, the configuration (model, prompt version, tools) as params, counts as metrics
and the resulting selection file as an artifact. The message's trace (every LLM and tool call) is
linked to that run, and runs with the same configuration share one LoggedModel, so they can be
compared and evaluated in the MLflow UI.

Runs are created with MlflowClient's explicit run IDs, not mlflow.start_run: the fluent active run
is process state, which concurrent requests on one event loop would share. Credentials never reach
logs or runs: they are split out of the URI and handed to MLflow through the environment
variables it reads itself.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import mlflow
from mlflow import MlflowClient
from mlflow.entities import LoggedModelOutput, Metric, Param

from app.settings import Settings

logger = logging.getLogger(__name__)


def split_credentials(uri: str) -> tuple[str, str | None, str | None]:
    """Pull `user:pass@` out of a tracking URI."""
    parts = urlsplit(uri)
    if parts.username is None:
        return uri, None, None
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit(parts._replace(netloc=netloc)), parts.username, parts.password


def tracking_uri(settings: Settings) -> str | None:
    """The tracking URI without credentials, safe to show."""
    if settings.mlflow_tracking_uri is None:
        return None
    return split_credentials(settings.mlflow_tracking_uri.get_secret_value().strip())[0] or None


def configure(settings: Settings) -> bool:
    """Point MLflow at the tracking server. False when none is configured."""
    if settings.mlflow_tracking_uri is None or not settings.mlflow_tracking_uri.get_secret_value().strip():
        return False
    uri, user, password = split_credentials(settings.mlflow_tracking_uri.get_secret_value().strip())
    if user is None and settings.mlflow_tracking_username:
        user = settings.mlflow_tracking_username
        password = settings.mlflow_tracking_password.get_secret_value() if settings.mlflow_tracking_password else None
    if user is not None:
        os.environ["MLFLOW_TRACKING_USERNAME"] = user
        os.environ["MLFLOW_TRACKING_PASSWORD"] = password or ""
    os.environ["MLFLOW_TRACKING_URI"] = uri  # MLflow's background threads read the variable
    mlflow.set_tracking_uri(uri)
    return True


@dataclass(frozen=True)
class QueryRun:
    run_id: str
    model_id: str | None


class RunLogger(Protocol):
    """Logs each agent message as a run. Implementations never raise: tracking must not stop the agent."""

    @property
    def tracing(self) -> bool: ...

    async def start(self, name: str, tags: dict[str, str], params: dict[str, str]) -> QueryRun | None: ...

    async def finish(
        self,
        run: QueryRun | None,
        *,
        message: str,
        reply: str,
        metrics: dict[str, float],
        artifact: Path | None,
        failed: bool = False,
    ) -> None: ...


class NullRunLogger:
    """Used when MLflow is not configured or unreachable."""

    tracing = False

    async def start(self, name: str, tags: dict[str, str], params: dict[str, str]) -> QueryRun | None:
        return None

    async def finish(self, run: QueryRun | None, **_: object) -> None:
        return None


class MlflowRunLogger:
    tracing = True

    def __init__(self, experiment_id: str, client: MlflowClient | None = None):
        self.experiment_id = experiment_id
        self.client = client or MlflowClient()
        # LoggedModel per configuration digest, so repeat configurations reuse one model
        self._models: dict[str, str] = {}

    async def start(self, name: str, tags: dict[str, str], params: dict[str, str]) -> QueryRun | None:
        try:
            return await asyncio.to_thread(self._start, name, tags, params)
        except Exception as exc:
            logger.warning("MLflow: cannot start a run for %s: %s", name, exc)
            return None

    def _start(self, name: str, tags: dict[str, str], params: dict[str, str]) -> QueryRun:
        run = self.client.create_run(self.experiment_id, tags=tags, run_name=name)
        run_id = run.info.run_id
        self.client.log_batch(run_id, params=[Param(k, str(v)[:6000]) for k, v in params.items()])
        model_id = self._model(params)
        if model_id:
            self.client.log_outputs(run_id, [LoggedModelOutput(model_id=model_id, step=0)])
        return QueryRun(run_id=run_id, model_id=model_id)

    def _model(self, params: dict[str, str]) -> str | None:
        """One LoggedModel per agent configuration, so runs and traces group by version.

        LoggedModel params are immutable, so the name carries a hash of the whole configuration:
        any change (prompt version, model, tools) gets a new model; an unchanged one is reused.
        """
        digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:8]
        if digest in self._models:
            return self._models[digest]
        name = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "-",
            f"{params.get('agent', 'agent')}-p{params.get('prompt_version', '?')}-{params.get('llm_model', '?')}-{digest}",
        )
        try:
            found = self.client.search_logged_models([self.experiment_id], filter_string=f"name = '{name}'", max_results=1)
            model = found[0] if found else self.client.create_logged_model(
                self.experiment_id, name=name, params=params, model_type="agent"
            )
        except Exception as exc:
            logger.warning("MLflow: cannot find or create LoggedModel %s: %s", name, exc)
            return None
        self._models[digest] = model.model_id
        return model.model_id

    async def finish(
        self,
        run: QueryRun | None,
        *,
        message: str,
        reply: str,
        metrics: dict[str, float],
        artifact: Path | None,
        failed: bool = False,
    ) -> None:
        if run is None:
            return
        try:
            await asyncio.to_thread(self._finish, run, message, reply, metrics, artifact, failed)
        except Exception as exc:
            logger.warning("MLflow: cannot finish run %s: %s", run.run_id, exc)

    def _finish(
        self, run: QueryRun, message: str, reply: str, metrics: dict[str, float], artifact: Path | None, failed: bool
    ) -> None:
        self.client.log_text(run.run_id, message, "message.txt")
        self.client.log_text(run.run_id, reply, "reply.txt")
        now = int(time.time() * 1000)
        self.client.log_batch(run.run_id, metrics=[Metric(k, float(v), now, 0) for k, v in metrics.items()])
        if artifact is not None and artifact.is_file():
            self.client.log_artifact(run.run_id, str(artifact), "selection")
        self.client.set_terminated(run.run_id, "FAILED" if failed else "FINISHED")


def start_tracing(settings: Settings) -> RunLogger:
    """Trace LangChain calls to the experiment and log each message as a run.

    Falls back to NullRunLogger when MLflow is not configured or cannot be reached; the backend
    still starts, and the agent reports the problem when it cannot load its prompt.
    """
    if not configure(settings):
        logger.warning("MLFLOW_TRACKING_URI is not set: the dataset agent cannot load its prompt")
        return NullRunLogger()
    try:
        experiment = mlflow.set_experiment(settings.mlflow_experiment)
    except Exception as exc:
        logger.warning(
            "MLflow at %s: cannot select experiment %r, so agent messages are not logged: %s",
            tracking_uri(settings),
            settings.mlflow_experiment,
            exc,
        )
        return NullRunLogger()
    mlflow.langchain.autolog(silent=True)
    logger.info("Logging agent messages to MLflow experiment %r at %s", settings.mlflow_experiment, tracking_uri(settings))
    return MlflowRunLogger(experiment.experiment_id)
