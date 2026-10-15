import json

import pytest
from langchain_core.tools import StructuredTool

from agent_harness.config import ConfigError, load_mcp_connections, split_credentials
from agent_harness.mcp_tools import select_tools


def test_split_credentials_moves_basic_auth_out_of_uri():
    assert split_credentials("https://alice:s3cret@mlflow.example.org/path") == (
        "https://mlflow.example.org/path",
        "alice",
        "s3cret",
    )


def test_split_credentials_keeps_port_and_plain_uris():
    assert split_credentials("http://u:p@localhost:5000")[0] == "http://localhost:5000"
    assert split_credentials("https://mlflow.example.org") == ("https://mlflow.example.org", None, None)


@pytest.fixture
def mcp_json(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "af": {
                        "type": "http",
                        "url": "https://mcp.example.org/mcp/",
                        "headers": {"Authorization": "Bearer ${TEST_TOKEN}"},
                    },
                    "local": {"command": "uvx", "args": ["some-server"]},
                }
            }
        )
    )
    return path


def test_mcp_connections_expand_env_vars(mcp_json, monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "abc")
    connections = load_mcp_connections(mcp_json)
    assert connections["af"] == {
        "transport": "streamable_http",
        "url": "https://mcp.example.org/mcp/",
        "headers": {"Authorization": "Bearer abc"},
    }
    assert connections["local"]["transport"] == "stdio"


def test_mcp_connections_select_servers(mcp_json, monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "abc")
    assert list(load_mcp_connections(mcp_json, ["af"])) == ["af"]
    with pytest.raises(ConfigError, match="nope"):
        load_mcp_connections(mcp_json, ["nope"])


def test_mcp_connections_missing_env_var(mcp_json, monkeypatch):
    monkeypatch.delenv("TEST_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="TEST_TOKEN"):
        load_mcp_connections(mcp_json, ["af"])


def _tool(name: str, read_only: bool) -> StructuredTool:
    return StructuredTool.from_function(
        func=lambda: None, name=name, description=name, metadata={"readOnlyHint": read_only}
    )


TOOLS = [
    _tool("fs_list", True),
    _tool("fs_read", True),
    _tool("condor_query_jobs", True),
    _tool("condor_remove_job", False),
]


def test_select_tools_by_glob():
    assert [t.name for t in select_tools(TOOLS, ["fs_*"])] == ["fs_list", "fs_read"]


def test_select_tools_drops_writes_unless_allowed():
    assert [t.name for t in select_tools(TOOLS, ["condor_*"])] == ["condor_query_jobs"]
    assert len(select_tools(TOOLS, ["condor_*"], allow_writes=True)) == 2


def test_select_tools_fails_when_nothing_matches():
    with pytest.raises(ConfigError, match="No read-only MCP tools"):
        select_tools(TOOLS, ["rucio_*"])
