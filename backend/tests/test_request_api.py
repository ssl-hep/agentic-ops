"""Run request API: the dataset graph against the fake GitLab, a scripted builder and a fake Spec-is-ready."""

from collections.abc import Iterator
from dataclasses import fields
from datetime import UTC, datetime

import pytest
from conftest import MC23, ScriptedModel, settings, tool_call
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.request_graph import RequestContext, build_request_graph
from app.main import app
from app.ops import SpecReadyService
from app.run_request import SpecReadyReceipt
from app.settings import get_settings
from app.setup_api import AgentModels, get_agent_models

BKG_R15540 = MC23.format(dsid="700786", rtag="r15540")
BKG_R16083 = MC23.format(dsid="700786", rtag="r16083")
BKG_R99999 = MC23.format(dsid="700787", rtag="r99999")
DATA15 = "data15_13TeV.periodAllYear.physics_Main.PhysCont.DAOD_PHYS.grp15_v01_p7266"


class FakeSpecReady(SpecReadyService):
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    async def spec_ready(self, request_id: str, datasets: list[str]) -> SpecReadyReceipt:
        self.calls.append((request_id, datasets))
        return SpecReadyReceipt(request_id=request_id, datasets=len(datasets), accepted_at=datetime.now(UTC))


@pytest.fixture
def spec_ready(client: TestClient) -> Iterator[FakeSpecReady]:
    fake = FakeSpecReady()
    app.state.request_graph = build_request_graph(InMemorySaver(), fake)
    app.dependency_overrides[get_settings] = lambda: settings()
    app.dependency_overrides[get_agent_models] = lambda: AgentModels()
    yield fake


def with_builder(builder: ScriptedModel) -> None:
    app.dependency_overrides[get_agent_models] = lambda: AgentModels(builder=builder)
    app.dependency_overrides[get_settings] = lambda: settings(llm_base_url="https://llm.example/v1", llm_model="m")


def act(client: TestClient, view: dict, **action) -> dict:
    response = client.post(f"/api/requests/{view['id']}/actions", json=action)
    assert response.status_code == 200, response.text
    return response.json()


def test_builder_queries_expand_to_datasets(client: TestClient, spec_ready: FakeSpecReady) -> None:
    draft = {
        "queries": [
            {
                "folder": "RadiativeDecays",
                "sample_list": "Run3_bkg",
                "skip": ["_r99999_"],
                "reason": "Run 3 background; r99999 is not a real campaign",
            },
            {"folder": "RadiativeDecays", "sample_list": "Run2_data", "reason": "The data the request asks for"},
        ],
        "assumptions": ["All campaigns"],
        "questions": ["Do you want mc23e too?"],
    }
    builder = ScriptedModel(
        replies=[
            tool_call("find_datasets", {"folder": "RadiativeDecays", "sample_list": "Run3_bkg", "skip": ["_r99999_"]}),
            AIMessage(content="That is the list."),
            tool_call("DatasetDraft", draft, "s1"),
        ]
    )
    with_builder(builder)
    view = client.post("/api/requests", json={"request": "Run 3 background and data for RadiativeDecays", "ref": "v1.0"}).json()

    tool_result = next(m for m in builder.calls[1] if isinstance(m, ToolMessage))
    assert "2 of 3 datasets match" in tool_result.content
    assert BKG_R99999 not in tool_result.content

    assert view["waiting"] and view["status"] == "needs_input"
    assert [(d["name"], d["source"], d["line"]) for d in view["datasets"]] == [
        (BKG_R15540, "RadiativeDecays/Run3_bkg", 2),
        (BKG_R16083, "RadiativeDecays/Run3_bkg", 3),
        (DATA15, "RadiativeDecays/Run2_data", 1),
    ]
    assert view["draft"]["questions"] == ["Do you want mc23e too?"]
    assert view["draft"]["prompt_version"] == "ew-kit-datasets/1"
    assert view["errors"] == []

    edited = act(client, view, action="edit", datasets=[BKG_R15540, DATA15, "user.x.extra.v1"])
    assert [d["name"] for d in edited["datasets"]] == [BKG_R15540, DATA15, "user.x.extra.v1"]
    assert edited["datasets"][0]["source"] == "RadiativeDecays/Run3_bkg"
    assert edited["datasets"][2]["source"] is None
    assert edited["history"][-1] == {**edited["history"][-1], "added": ["user.x.extra.v1"], "removed": [BKG_R16083]}

    done = act(client, edited, action="finish")
    assert done["status"] == "ready" and not done["waiting"]
    assert done["receipt"]["datasets"] == 3
    assert spec_ready.calls == [(view["id"], [BKG_R15540, DATA15, "user.x.extra.v1"])]
    assert client.post(f"/api/requests/{view['id']}/actions", json={"action": "finish"}).status_code == 409


def test_bad_queries_are_errors_not_crashes(client: TestClient, spec_ready: FakeSpecReady) -> None:
    draft = {
        "queries": [
            {"folder": "RadiativeDecays", "sample_list": "Run9_bkg", "reason": "x"},
            {"folder": "RadiativeDecays", "sample_list": "Run3_bkg", "include": ["("], "reason": "x"},
            {"folder": "RadiativeDecays", "sample_list": "Run3_bkg", "include": ["nomatch"], "reason": "x"},
        ],
        "extra_datasets": [DATA15 + "/"],
    }
    with_builder(ScriptedModel(replies=[AIMessage(content="Done."), tool_call("DatasetDraft", draft, "s1")]))
    view = client.post("/api/requests", json={"request": "something", "ref": "v1.0"}).json()
    assert [d["name"] for d in view["datasets"]] == [DATA15]
    assert len(view["errors"]) == 3
    assert "Run9_bkg" in view["errors"][0]
    assert "invalid" in view["errors"][1]
    assert "leave no datasets" in view["errors"][2]


def test_without_builder_the_person_lists_datasets(client: TestClient, spec_ready: FakeSpecReady) -> None:
    view = client.post("/api/requests", json={"analysis": "RadiativeDecays"}).json()
    assert view["waiting"] and view["datasets"] == [] and view["agents_enabled"] is False
    assert view["resolved"]["ref"] == "master"

    assert client.post(f"/api/requests/{view['id']}/actions", json={"action": "finish"}).status_code == 409
    assert (
        client.post(f"/api/requests/{view['id']}/actions", json={"action": "redraft", "request": "x"}).status_code
        == 409
    )

    edited = act(client, view, action="edit", datasets=[DATA15, " ", DATA15])
    assert [d["name"] for d in edited["datasets"]] == [DATA15]
    done = act(client, edited, action="finish")
    assert done["status"] == "ready"
    assert spec_ready.calls == [(view["id"], [DATA15])]
    assert client.get("/api/requests/nope").status_code == 404


def test_spec_ready_is_not_in_the_model_context() -> None:
    """Model-calling nodes see only the run context; the procedural service must not be in it."""
    assert {f.name for f in fields(RequestContext)} == {"repo", "settings", "prompts", "builder"}
