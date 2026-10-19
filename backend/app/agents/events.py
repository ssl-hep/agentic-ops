"""History events and error entries shared by the agent graphs' nodes."""

from datetime import UTC, datetime
from typing import Any

from app.gitlab import GitLabError


def event(kind: str, **details: Any) -> dict:
    return {"event": kind, "at": datetime.now(UTC).isoformat(timespec="seconds"), **details}


def failed(state: dict, step: str, exc: Exception) -> dict:
    message = exc.message if isinstance(exc, GitLabError) else str(exc)
    return {"errors": [*state.get("errors", []), f"{step}: {message}"], "history": [event("error", step=step)]}
