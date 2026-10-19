"""MLflow: the agents' system prompts come from its Prompt Registry, and every agent run is traced there.

The tracking server and experiment come from settings (MLFLOW_TRACKING_URI, MLFLOW_EXPERIMENT).
Credentials never reach logs or traces: they are split out of the URI and handed to MLflow through
the environment variables it reads itself.
"""

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

import mlflow
from mlflow.entities import SpanType
from mlflow.tracing.constant import TraceMetadataKey

from app.settings import Settings

logger = logging.getLogger(__name__)

# Set by start_tracing; MLflow's own configuration is process-wide too
_tracing = False


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


def start_tracing(settings: Settings) -> str | None:
    """Trace every LangGraph and model call to the experiment, creating it if needed.

    Returns the experiment ID, or None when MLflow is not configured or cannot be reached; the
    backend still starts, and the agents report the problem when they cannot load a prompt.
    """
    if not configure(settings):
        logger.warning("MLFLOW_TRACKING_URI is not set: the agents cannot load their prompts")
        return None
    try:
        experiment = mlflow.set_experiment(settings.mlflow_experiment)
    except Exception as exc:
        logger.warning(
            "MLflow at %s: cannot select experiment %r, so agent runs are not traced: %s",
            tracking_uri(settings),
            settings.mlflow_experiment,
            exc,
        )
        return None
    mlflow.langchain.autolog(silent=True)
    global _tracing
    _tracing = True
    logger.info("Tracing agent runs to MLflow experiment %r at %s", settings.mlflow_experiment, tracking_uri(settings))
    return experiment.experiment_id


@contextmanager
def graph_run(name: str, thread_id: str, inputs: dict) -> Iterator[None]:
    """One trace per graph run, grouped into a session per setup or request.

    Autologged LangGraph spans nest under this span. It also gives the nodes an active trace, which
    they lack under autolog alone, so each prompt version they load is linked to the trace.
    """
    if not _tracing:
        yield
        return
    with mlflow.start_span(name=name, span_type=SpanType.AGENT) as span:
        span.set_inputs({"id": thread_id, **inputs})
        mlflow.update_current_trace(metadata={TraceMetadataKey.TRACE_SESSION: thread_id})
        yield
