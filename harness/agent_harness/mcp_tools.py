"""MCP tools for the agent, cut down to an allowlist.

The AF gateway exposes every service it fronts, including destructive ones (condor_remove_job,
jlab_delete_jupyter_server), so the agent only ever sees tools that match the allowlist.
"""

from collections.abc import Sequence
from fnmatch import fnmatchcase

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from agent_harness.config import ConfigError


def is_read_only(tool: BaseTool) -> bool:
    """MCP's readOnlyHint, which the adapter copies into the tool metadata."""
    return bool((tool.metadata or {}).get("readOnlyHint"))


def select_tools(tools: Sequence[BaseTool], allow: Sequence[str], allow_writes: bool = False) -> list[BaseTool]:
    """Tools whose name matches one of the `allow` globs, read-only ones only unless `allow_writes`."""
    chosen = [t for t in tools if any(fnmatchcase(t.name, pattern) for pattern in allow)]
    if not allow_writes:
        chosen = [t for t in chosen if is_read_only(t)]
    if not chosen:
        raise ConfigError(
            f"No {'' if allow_writes else 'read-only '}MCP tools match {list(allow)}; "
            f"{len(tools)} tools are available"
        )
    return chosen


async def load_tools(connections: dict[str, dict], allow: Sequence[str], allow_writes: bool = False) -> list[BaseTool]:
    tools = await MultiServerMCPClient(connections).get_tools()
    return select_tools(tools, allow, allow_writes)
