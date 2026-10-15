"""Environment, MLflow and MCP configuration for the harness.

Everything comes from the repo-root .env and mcp.json, so the harness and Claude Code share one
MCP server definition.
"""

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import mlflow
from dotenv import load_dotenv

DEFAULT_EXPERIMENT = "agentic-ops"


class ConfigError(Exception):
    """The environment or mcp.json is missing something the harness needs. The message is safe to show."""


def find_upwards(name: str, start: Path | None = None) -> Path | None:
    """The first `name` in `start` or one of its parents, so the harness runs from any subdirectory."""
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        if (directory / name).is_file():
            return directory / name
    return None


def load_env(env_file: Path | None = None) -> None:
    path = env_file or find_upwards(".env")
    if path is None:
        raise ConfigError("No .env found; copy harness/.env.example to the repo root as .env")
    load_dotenv(path, override=False)


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set; add it to .env")
    return value


def split_credentials(uri: str) -> tuple[str, str | None, str | None]:
    """Pull `user:pass@` out of a tracking URI, so credentials never land in logged run metadata."""
    parts = urlsplit(uri)
    if parts.username is None:
        return uri, None, None
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit(parts._replace(netloc=netloc)), parts.username, parts.password


def configure_mlflow(experiment: str = DEFAULT_EXPERIMENT) -> str:
    """Point MLflow at the tracking server from .env and select the experiment. Returns the clean URI."""
    uri, user, password = split_credentials(require("MLFLOW_TRACKING_URI"))
    if user is not None:
        os.environ.setdefault("MLFLOW_TRACKING_USERNAME", user)
        os.environ.setdefault("MLFLOW_TRACKING_PASSWORD", password or "")
    os.environ["MLFLOW_TRACKING_URI"] = uri  # child processes and autolog read the env var
    mlflow.set_tracking_uri(uri)
    mlflow.set_experiment(experiment)
    return uri


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand(value: str) -> str:
    def lookup(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ConfigError(f"mcp.json references ${{{name}}}, which is not set; add it to .env")
        return os.environ[name]

    return _VAR.sub(lookup, value)


def _connection(name: str, server: dict) -> dict:
    kind = server.get("type") or ("stdio" if "command" in server else "http")
    if kind in ("http", "streamable_http", "streamable-http"):
        return {
            "transport": "streamable_http",
            "url": expand(server["url"]),
            "headers": {k: expand(v) for k, v in server.get("headers", {}).items()},
        }
    if kind == "sse":
        return {
            "transport": "sse",
            "url": expand(server["url"]),
            "headers": {k: expand(v) for k, v in server.get("headers", {}).items()},
        }
    if kind == "stdio":
        return {
            "transport": "stdio",
            "command": server["command"],
            "args": [expand(a) for a in server.get("args", [])],
            "env": {k: expand(v) for k, v in server.get("env", {}).items()} or None,
        }
    raise ConfigError(f"MCP server {name!r} has unsupported type {kind!r}")


def load_mcp_connections(path: Path | None = None, servers: list[str] | None = None) -> dict[str, dict]:
    """Connections for langchain-mcp-adapters from a Claude-style mcp.json, with ${VAR}s expanded."""
    path = path or find_upwards("mcp.json")
    if path is None:
        raise ConfigError("No mcp.json found")
    defined = json.loads(path.read_text()).get("mcpServers", {})
    wanted = servers or list(defined)
    unknown = sorted(set(wanted) - set(defined))
    if unknown:
        raise ConfigError(f"MCP servers {unknown} are not in {path}; it defines {sorted(defined)}")
    return {name: _connection(name, defined[name]) for name in wanted}
