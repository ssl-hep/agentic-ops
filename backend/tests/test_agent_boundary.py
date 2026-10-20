"""The dataset agent's boundary: what it can call, and what its code can reach.

The agent acts only through its tools, so these tests pin that list and the code behind it. The
Kerberos ticket lives in a sidecar container the backend can't see into (see "Kerberos" in the
README). These tests make sure Kerberos-backed code is never wired into the agent: a new tool, or a
new import from agent code, fails here until someone changes this file on purpose.
"""

import ast
import asyncio
from pathlib import Path

from langchain_core.messages import AIMessage

from app.agents.narrower import Narrower
from app.selections import Selection
from app.settings import Settings
from tests.test_selections import FakePrompt, FakeRuns, ScriptedModel, fake_replica_tools, selection, store  # noqa: F401

APP = Path(__file__).resolve().parents[1] / "app"

# Every tool the agent can call besides the allowlisted read-only MCP tools. No tool may call
# Kerberos-backed code; see "Kerberos" in the README before adding one.
AGENT_TOOLS = {
    "summarize_datasets",
    "list_datasets",
    "select_datasets",
    "deselect_datasets",
    "keep_only",
    "select_by_name",
    "deselect_by_name",
    "show_selection",
    "clear_selection",
    "check_replicas",
}

# Modules that run processes or use Kerberos credentials. Agent code must not import them, directly
# or through other app modules.
FORBIDDEN_IMPORTS = {"subprocess", "gssapi", "krb5", "kerberos", "requests_kerberos", "requests_gssapi", "httpx_gssapi"}
FORBIDDEN_APP_MODULES = ("app.kerberos",)


def test_the_agent_gets_exactly_the_pinned_tools(selection: Selection) -> None:
    gateway, _ = fake_replica_tools({})

    async def mcp() -> tuple[list, str | None]:
        return gateway, None

    runs = FakeRuns()
    model = ScriptedModel(replies=[AIMessage(content="Nothing to do.")])
    narrower = Narrower(Settings(_env_file=None, llm_model="test-model"), model, FakePrompt(), mcp, runs)
    asyncio.run(narrower.answer(selection, "hello"))

    (started,) = runs.started
    assert set(started["params"]["tools"].split(",")) == AGENT_TOOLS | {t.name for t in gateway}


def module_name(path: Path) -> str:
    parts = path.relative_to(APP.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def imports_of(path: Path) -> set[str]:
    """Every module `path` imports, with `from app import x` counted as importing app.x."""
    package = module_name(path) if path.name == "__init__.py" else module_name(path).rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")[: len(package.split(".")) - node.level + 1]
                base = ".".join([*anchor, base] if base else anchor)
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def test_agent_code_cannot_reach_kerberos_or_processes() -> None:
    graph = {module_name(p): imports_of(p) for p in APP.rglob("*.py")}
    reached: set[str] = set()
    todo = [m for m in graph if m == "app.agents" or m.startswith("app.agents.")]
    while todo:
        if (module := todo.pop()) in reached:
            continue
        reached.add(module)
        todo.extend(m for m in graph[module] if m in graph)

    # Today: app.agents.*, settings, selections, repository, gitlab, ewkit, schemas, tracking
    assert "app.main" not in reached
    for module in sorted(reached):
        bad = {m for m in graph[module] if m.startswith(FORBIDDEN_APP_MODULES) or m.split(".")[0] in FORBIDDEN_IMPORTS}
        assert not bad, f"agent code reaches {module}, which imports {sorted(bad)}"


def test_kerberos_settings_stay_out_of_the_settings_the_agent_holds() -> None:
    assert not [name for name in Settings.model_fields if "krb" in name or "kerberos" in name]
