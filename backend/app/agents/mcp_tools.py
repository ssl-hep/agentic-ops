"""Rucio tools from the AF MCP gateway, cut down to an allowlist of read-only tools.

The gateway exposes every service it fronts, including destructive ones (condor_remove_job,
jlab_delete_jupyter_server), so the agent only ever sees tools that match the allowlist and carry
MCP's readOnlyHint.
"""

import logging
from collections.abc import Sequence
from fnmatch import fnmatchcase

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from app.settings import Settings

logger = logging.getLogger(__name__)

SERVER = "af"


def is_read_only(tool: BaseTool) -> bool:
    """MCP's readOnlyHint, which the adapter copies into the tool metadata."""
    return bool((tool.metadata or {}).get("readOnlyHint"))


def select_tools(tools: Sequence[BaseTool], allow: Sequence[str]) -> list[BaseTool]:
    """Read-only tools whose name matches one of the `allow` globs."""
    return [t for t in tools if is_read_only(t) and any(fnmatchcase(t.name, pattern) for pattern in allow)]


def connections(settings: Settings) -> dict[str, dict]:
    if not settings.mcp_url:
        return {}
    headers = {}
    if settings.mcp_bearer_token:
        headers["Authorization"] = f"Bearer {settings.mcp_bearer_token.get_secret_value().strip()}"
    return {SERVER: {"transport": "streamable_http", "url": settings.mcp_url, "headers": headers}}


async def load_tools(settings: Settings) -> tuple[list[BaseTool], str | None]:
    """The allowlisted Rucio tools, and a note for the model when they are unavailable.

    The gateway being down must not stop dataset narrowing, so failures become a note, not an error.
    """
    conns = connections(settings)
    if not conns:
        return [], "Rucio tools are not configured (no MCP_URL), so replica and rule checks are unavailable."
    try:
        tools = await MultiServerMCPClient(conns).get_tools()
    except Exception as exc:
        logger.warning("MCP gateway %s: cannot list tools: %s", settings.mcp_url, exc)
        return [], "The Rucio tools (AF MCP gateway) are unreachable right now, so replica and rule checks are unavailable."
    chosen = select_tools(tools, settings.mcp_tool_allowlist)
    if not chosen:
        logger.warning("MCP gateway: no read-only tools match %s among %d", settings.mcp_tool_allowlist, len(tools))
        return [], "No read-only Rucio tools are available from the AF MCP gateway."
    return chosen, None
