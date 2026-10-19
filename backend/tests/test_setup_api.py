"""Run setup API: the LangGraph graph against the fake GitLab, with scripted chat models."""

from collections.abc import Iterator

import pytest
from conftest import ScriptedModel, settings, tool_call
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

from app.main import app
from app.settings import get_settings
from app.setup_api import AgentModels, get_agent_models


@pytest.fixture
def setup_client(client: TestClient) -> Iterator[TestClient]:
    app.dependency_overrides[get_settings] = lambda: settings()
    app.dependency_overrides[get_agent_models] = lambda: AgentModels()
    yield client


def with_models(builder: ScriptedModel | None, reviewer: ScriptedModel | None) -> None:
    app.dependency_overrides[get_agent_models] = lambda: AgentModels(builder=builder, reviewer=reviewer)
    app.dependency_overrides[get_settings] = lambda: settings(llm_base_url="https://llm.example/v1", llm_model="m")


def codes(view: dict) -> set[str]:
    return {f["code"] for f in view["report"]["findings"]}


def test_form_first_setup_without_agents(setup_client: TestClient) -> None:
    view = setup_client.post("/api/setups", json={"analysis": "RadiativeDecays", "ref": "v1.0"}).json()
    assert view["waiting"] and view["status"] == "needs_input"
    assert view["agents_enabled"] is False
    assert view["resolved"]["ref"] == "v1.0"
    spec = view["spec"]
    assert spec["runs"] == ["Run2", "Run3"]
    assert spec["sample_lists"] == ["bkg", "data", "signal"]
    assert spec["purpose"] == "production"  # v1.0 is a tag
    assert "UNKNOWN_RTAG" in codes(view)
    assert view["issues"] == []

    finish = setup_client.post(f"/api/setups/{view['id']}/actions", json={"action": "finish"})
    assert finish.status_code == 409

    redraft = setup_client.post(f"/api/setups/{view['id']}/actions", json={"action": "redraft", "request": "x"})
    assert redraft.status_code == 409

    edited = setup_client.post(
        f"/api/setups/{view['id']}/actions",
        json={"action": "edit", "spec": {**spec, "skip": ["_r99999_"]}},
    ).json()
    assert "UNKNOWN_RTAG" not in codes(edited)
    assert edited["blocking"] == 0
    assert [e["event"] for e in edited["history"]] == ["created", "edited"]

    done = setup_client.post(f"/api/setups/{view['id']}/actions", json={"action": "finish"}).json()
    assert done["status"] == "ready" and not done["waiting"]
    again = setup_client.post(f"/api/setups/{view['id']}/actions", json={"action": "finish"})
    assert again.status_code == 409

    assert setup_client.get(f"/api/setups/{view['id']}").json()["status"] == "ready"
    assert setup_client.get("/api/setups/nope").status_code == 404


def test_builder_and_reviewer(setup_client: TestClient) -> None:
    draft = {
        "spec": {"analysis": "RadiativeDecays", "ref": "v1.0", "runs": ["Run3"], "sample_lists": ["bkg"], "systematics": "do"},
        "reasons": [{"field": "runs", "reason": "The request asks for Run 3"}],
        "assumptions": ["Production from tag v1.0"],
        "questions": [],
    }
    builder = ScriptedModel(
        replies=[
            tool_call("get_analysis", {"folder": "RadiativeDecays"}),
            AIMessage(content="I have what I need."),
            tool_call("BuilderDraft", draft, "s1"),
        ]
    )
    rtag_card = {
        "severity": "blocking",
        "title": "One Run 3 background dataset has an unknown r-tag",
        "explanation": "Its output cannot be named.",
        "finding_codes": ["UNKNOWN_RTAG"],
        "patch": {"skip": ["_r99999_"]},
        "if_declined": "The input checks cannot pass.",
    }
    reviewer = ScriptedModel(
        replies=[
            AIMessage(content="Reviewing."),
            tool_call(
                "ReviewerOutput",
                {
                    "issues": [
                        rtag_card,
                        {"severity": "warning", "title": "No evidence", "explanation": "Made up."},
                        {
                            "severity": "warning",
                            "title": "Cites a missing file",
                            "explanation": "x",
                            "evidence": [{"path": "data/Nope/config.yaml"}],
                        },
                        {
                            "severity": "warning",
                            "title": "No image",
                            "explanation": "CI has not pushed an image for v1.0; this card gets no patch.",
                            "finding_codes": ["NO_IMAGE_FOR_REF"],
                        },
                        {
                            "severity": "info",
                            "title": "Bad patch",
                            "explanation": "x",
                            "finding_codes": ["NO_IMAGE_FOR_REF"],
                            "patch": {"memory_mb": 10},
                        },
                    ]
                },
                "s2",
            ),
            # Second review, after the patch is accepted
            AIMessage(content="Reviewing again."),
            tool_call("ReviewerOutput", {"issues": []}, "s3"),
        ]
    )
    with_models(builder, reviewer)

    view = setup_client.post(
        "/api/setups", json={"request": "Run 3 background for RadiativeDecays at v1.0, with systematics"}
    ).json()
    assert view["errors"] == []
    assert view["spec"]["systematics"] == "do"
    assert view["draft"]["reasons"] == {"runs": "The request asks for Run 3"}
    assert view["draft"]["model"] == "m"
    assert view["models"]["reviewer"]["prompt_version"] == "reviewer-2"

    # Tool results reach the model wrapped as evidence
    tool_messages = [m for m in builder.calls[1] if isinstance(m, ToolMessage)]
    assert tool_messages[0].content.startswith('<evidence source="data/RadiativeDecays@')
    # The reviewer never sees the builder's conversation (D29)
    assert not any(isinstance(m, ToolMessage) for m in reviewer.calls[0])

    titles = [c["title"] for c in view["issues"]]
    assert titles == [rtag_card["title"], "No image", "Bad patch"]  # cards without evidence are dropped
    card, no_image, bad = view["issues"]
    assert no_image["patch"] is None
    assert card["effect"]["clears"] == ["UNKNOWN_RTAG:Run3_bkg:r99999"]
    assert card["owners"] == ["@bhodkins"]
    assert card["evidence"][0]["path"] == "data/RadiativeDecays/DAODs/Run3_bkg.txt"
    assert "memory_mb" in bad["patch_error"]

    bad_id = setup_client.post(f"/api/setups/{view['id']}/actions", json={"action": "apply", "accept": ["nope"]})
    assert bad_id.status_code == 422

    after = setup_client.post(
        f"/api/setups/{view['id']}/actions", json={"action": "apply", "accept": [card["id"]], "dismiss": [bad["id"], no_image["id"]]}
    ).json()
    assert after["spec"]["skip"] == ["_r99999_"]
    assert "UNKNOWN_RTAG" not in codes(after)
    assert after["issues"] == []
    events = [e["event"] for e in after["history"]]
    assert events == ["created", "drafted", "reviewed", "accepted", "dismissed", "dismissed", "reviewed"]


def test_blocking_findings_always_get_a_card(setup_client: TestClient) -> None:
    reviewer = ScriptedModel(replies=[AIMessage(content="Looks fine."), tool_call("ReviewerOutput", {"issues": []})])
    with_models(ScriptedModel(replies=[]), reviewer)
    view = setup_client.post("/api/setups", json={"analysis": "RadiativeDecays", "ref": "v1.0"}).json()
    [card] = view["issues"]
    assert card["source"] == "validator"
    assert card["finding_codes"] == ["UNKNOWN_RTAG"]
    assert card["evidence"][0]["line_numbers"] == [5]
    # The final instruction names the blocking findings
    assert "Every blocking finding needs a card: UNKNOWN_RTAG" in reviewer.calls[-1][-1].content
    # Code suggests the mechanical fix, dry-run like a reviewer patch
    assert card["patch"] == {"skip": ["_r99999_"]}
    assert card["effect"]["clears"] == ["UNKNOWN_RTAG:Run3_bkg:r99999"]

    reviewer.replies += [AIMessage(content="Fine now."), tool_call("ReviewerOutput", {"issues": []})]
    after = setup_client.post(f"/api/setups/{view['id']}/actions", json={"action": "apply", "accept": [card["id"]]}).json()
    assert after["spec"]["skip"] == ["_r99999_"]
    assert after["blocking"] == 0 and after["issues"] == []


def test_reviewer_card_without_patch_gets_the_suggestion(setup_client: TestClient) -> None:
    issue = {"severity": "blocking", "title": "Unknown r-tag", "explanation": "x", "finding_codes": ["UNKNOWN_RTAG"]}
    reviewer = ScriptedModel(replies=[AIMessage(content="."), tool_call("ReviewerOutput", {"issues": [issue]})])
    with_models(ScriptedModel(replies=[]), reviewer)
    [card] = setup_client.post("/api/setups", json={"analysis": "RadiativeDecays", "ref": "v1.0"}).json()["issues"]
    assert card["source"] == "reviewer"
    assert card["patch"] == {"skip": ["_r99999_"]}


def test_model_failure_is_shown_not_raised(setup_client: TestClient) -> None:
    builder = ScriptedModel(
        replies=[
            AIMessage(content="no tools needed"),
            tool_call("get_analysis", {"folder": "RadiativeDecays"}),  # ignores the named tool_choice
            AIMessage(content="still no structured answer"),  # the retry
        ]
    )
    with_models(builder, None)
    view = setup_client.post("/api/setups", json={"request": "something", "analysis": "RadiativeDecays"}).json()
    assert view["waiting"]
    assert view["spec"] is None
    assert view["errors"][0].startswith("Builder: The model did not return a valid BuilderDraft")
    assert "No other function is available" in builder.calls[-1][-1].content
    assert builder.replies == []


def test_agents_status(setup_client: TestClient) -> None:
    body = setup_client.get("/api/setups/agents").json()
    assert body["enabled"] is False and body["builder_model"] is None

    app.dependency_overrides[get_settings] = lambda: settings(
        llm_base_url="https://llm.example/v1", llm_model="big", llm_reviewer_model="small", llm_api_key="secret"
    )
    body = setup_client.get("/api/setups/agents").json()
    assert body == {
        "enabled": True,
        "base_url": "https://llm.example/v1",
        "builder_model": "big",
        "reviewer_model": "small",
        "structured_output": "function_calling",
        "api_key_configured": True,
        "reachable": None,
        "detail": None,
    }
