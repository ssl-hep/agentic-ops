"""Dataset agent: catalog filters, selection tools, the scratch file, the API and its MLflow logging."""

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from mlflow.entities.model_registry import PromptVersion

from app.agents.dataset_tools import dataset_tools
from app.agents.mcp_tools import select_tools
from app.agents.narrower import Narrower
from app.agents.prompts import SystemPrompt, render
from app.api import get_repository
from app.gitlab import ReadOnlyGitLabClient
from app.main import app
from app.repository import EWKitRepository, NotFoundError
from app.selection_api import get_narrower, get_store
from app.selections import Filters, Selection, SelectionStore
from app.settings import Settings
from app.tracking import QueryRun, split_credentials
from tests.test_api import MC23, PROJECT, fake_gitlab

FOLDER = "RadiativeDecays"
MC23A = MC23.format(dsid="700786", rtag="r15540")
MC23E = MC23.format(dsid="700786", rtag="r16083")
ODD = MC23.format(dsid="700787", rtag="r99999")
DATA15 = "data15_13TeV.periodAllYear.physics_Main.PhysCont.DAOD_PHYS.grp15_v01_p7266"


def make_repository() -> EWKitRepository:
    gitlab = ReadOnlyGitLabClient(
        "https://gitlab.example", PROJECT, "test-token", transport=httpx.MockTransport(fake_gitlab)
    )
    return EWKitRepository(gitlab)


@pytest.fixture
def store(tmp_path: Path) -> SelectionStore:
    return SelectionStore(tmp_path)


@pytest.fixture
def selection(store: SelectionStore) -> Selection:
    return asyncio.run(store.create(make_repository(), FOLDER, None))


def tools(selection: Selection) -> dict[str, StructuredTool]:
    return {t.name: t for t in dataset_tools(selection)}


def call(selection: Selection, name: str, **args: Any) -> str:
    return asyncio.run(tools(selection)[name].ainvoke(args))


# ----- catalog and filters -------------------------------------------------------------


def test_catalog_has_every_daod_dataset_with_run_sample_and_campaign(selection: Selection) -> None:
    entries = {e.name: e for e in selection.catalog.entries}
    assert set(entries) == {MC23A, MC23E, ODD, DATA15}
    assert (entries[MC23E].run, entries[MC23E].sample, entries[MC23E].campaign) == ("Run3", "bkg", "mc23e")
    assert entries[MC23A].heading == "Vjets: Z+jets"
    assert (entries[DATA15].run, entries[DATA15].sample, entries[DATA15].campaign) == ("Run2", "data", "data15")


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (Filters.of(runs=["Run3"]), {MC23A, MC23E, ODD}),
        (Filters.of(runs=["2"]), {DATA15}),
        (Filters.of(samples=["data"]), {DATA15}),
        (Filters.of(samples=["run3_BKG"]), {MC23A, MC23E, ODD}),
        (Filters.of(campaigns=["MC23E", "data15"]), {MC23E, DATA15}),
        (Filters.of(runs=["Run3"], campaigns=["unknown"]), {ODD}),
        (Filters.of(contains="z+jets"), {MC23A, MC23E}),
        (Filters.of(dsids=["700787"]), {ODD}),
        (Filters.of(), {MC23A, MC23E, ODD, DATA15}),
    ],
)
def test_filters(selection: Selection, filters: Filters, expected: set[str]) -> None:
    assert {e.name for e in selection.catalog.find(filters)} == expected


def test_unknown_folder_is_not_found(store: SelectionStore) -> None:
    with pytest.raises(NotFoundError):
        asyncio.run(store.create(make_repository(), "Nope", None))


@pytest.mark.parametrize(("folder", "selection_id"), [("../etc", str(uuid.uuid4())), (FOLDER, "../../x"), (".hidden", str(uuid.uuid4()))])
def test_scratch_paths_cannot_escape(store: SelectionStore, folder: str, selection_id: str) -> None:
    with pytest.raises(NotFoundError):
        store.path_for(folder, selection_id)


# ----- tools ---------------------------------------------------------------------------


def test_list_datasets_counts_and_caps(selection: Selection) -> None:
    out = call(selection, "list_datasets", runs=["Run3"], limit=1)
    assert "3 datasets match; showing 1" in out
    assert out.startswith('<evidence source="data/RadiativeDecays/DAODs@')


def test_select_then_keep_only_then_deselect(selection: Selection) -> None:
    assert "added 3" in call(selection, "select_datasets", runs=["Run3"])
    assert selection.selected == {MC23A, MC23E, ODD}
    assert "removed 2" in call(selection, "keep_only", campaigns=["mc23e"])
    assert selection.selected == {MC23E}
    assert "Removed 1" in call(selection, "deselect_datasets", campaigns=["mc23e"])
    assert selection.selected == set()


def test_select_by_name_accepts_scope_and_rejects_names_outside_the_lists(selection: Selection) -> None:
    out = call(selection, "select_by_name", names=[f"mc23_13p6TeV:{MC23E}/", "mc23_13p6TeV.999999.Made.Up"])
    assert "Added 1" in out
    assert "1 names are not in this analysis's DAODs lists" in out
    assert selection.selected == {MC23E}


def test_scratch_file_is_a_grouped_subset_in_file_order(selection: Selection) -> None:
    call(selection, "select_datasets", campaigns=["data15", "mc23e", "unknown"])
    lines = selection.path.read_text().splitlines()
    assert lines[0].startswith(f"# Dataset selection {selection.id} for data/{FOLDER} at master")
    # Lists sort Run2 before Run3, and groups keep their headings
    assert lines[1:] == ["# Run2_data", DATA15, "# Run3_bkg: Vjets: Z+jets", MC23E, "# Run3_bkg: odd", ODD]
    datasets = [line for line in lines if not line.startswith("#")]
    assert set(datasets) <= {e.name for e in selection.catalog.entries}
    assert not list(selection.path.parent.glob("*.tmp"))


# ----- MCP allowlist -------------------------------------------------------------------


def _tool(name: str, read_only: bool) -> StructuredTool:
    async def run() -> str:
        return ""

    return StructuredTool.from_function(coroutine=run, name=name, description=name, metadata={"readOnlyHint": read_only})


def test_mcp_tools_are_allowlisted_and_read_only() -> None:
    tools = [
        _tool("rucio_list_replicas", True),
        _tool("rucio_get_did", True),
        _tool("rucio_add_rule", False),
        _tool("rucio_list_unsafe", False),
        _tool("condor_remove_job", False),
        _tool("fs_list", True),
    ]
    chosen = select_tools(tools, ["rucio_list*", "rucio_get*"])
    assert [t.name for t in chosen] == ["rucio_list_replicas", "rucio_get_did"]


def test_split_credentials() -> None:
    assert split_credentials("https://u:p%40ss@mlflow.example:8443/x") == ("https://mlflow.example:8443/x", "u", "p%40ss")
    assert split_credentials("https://mlflow.example") == ("https://mlflow.example", None, None)


# ----- API with a scripted model -------------------------------------------------------


class ScriptedModel(BaseChatModel):
    """Returns the scripted replies in order and records what it was sent."""

    replies: list[AIMessage]
    calls: list[list[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedModel":
        return self

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        self.calls.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=self.replies.pop(0))])


def tool_call(name: str, args: dict, id: str = "call-1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id}])


class FakePrompt:
    async def load(self, **variables: str) -> SystemPrompt:
        return render(PromptVersion("ew-kit-dataset-narrower", 3, "Narrow data/{{ folder }} at {{ sha }}."), **variables)


class FakeRuns:
    """Records the runs the agent would log to MLflow."""

    tracing = False

    def __init__(self) -> None:
        self.started: list[dict] = []
        self.finished: list[dict] = []

    async def start(self, name: str, tags: dict[str, str], params: dict[str, str]) -> QueryRun:
        self.started.append({"name": name, "tags": tags, "params": params})
        return QueryRun(run_id=f"run-{len(self.started)}", model_id="m-1")

    async def finish(self, run: QueryRun | None, **kwargs: Any) -> None:
        artifact = kwargs.get("artifact")
        self.finished.append({"run": run, **kwargs, "artifact_text": artifact.read_text() if artifact else None})


async def no_mcp() -> tuple[list, str | None]:
    return [], "Rucio tools are not configured."


@pytest.fixture
def agent_client(tmp_path: Path) -> Iterator[tuple[TestClient, ScriptedModel, FakeRuns]]:
    model = ScriptedModel(
        replies=[
            tool_call("select_datasets", {"runs": ["Run3"], "campaigns": ["mc23e"]}),
            AIMessage(content="Selected 1 dataset: Run3 bkg, mc23e."),
        ]
    )
    runs = FakeRuns()
    narrower = Narrower(Settings(_env_file=None, llm_model="test-model"), model, FakePrompt(), no_mcp, runs)
    repository = make_repository()
    selection_store = SelectionStore(tmp_path)
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_store] = lambda: selection_store
    app.dependency_overrides[get_narrower] = lambda: narrower
    with TestClient(app) as test_client:
        yield test_client, model, runs
    app.dependency_overrides.clear()


def test_chat_narrows_the_selection_writes_the_file_and_logs_a_run(agent_client) -> None:
    client, model, runs = agent_client
    base = f"/api/ewkit/analyses/{FOLDER}/selections"
    created = client.post(base, json={"ref": "master"}).json()
    assert created["selection"]["count"] == 0
    assert created["available"]["count"] == 4

    reply = client.post(f"{base}/{created['id']}/messages", json={"content": "Only Run3 mc23e"}).json()
    assert reply["reply"] == "Selected 1 dataset: Run3 bkg, mc23e."
    assert reply["selection"]["count"] == 1
    assert reply["selection"]["by_campaign"] == {"mc23e": 1}
    assert reply["tool_calls"] == [{"name": "select_datasets", "args": {"runs": ["Run3"], "campaigns": ["mc23e"]}}]
    assert reply["mlflow_run_id"] == "run-1"

    # The prompt was filled with the folder, and the Rucio note reached the model
    system = model.calls[0][0].content
    assert f"Narrow data/{FOLDER} at" in system and "Rucio tools are not configured" in system

    # One run per message, with the configuration as params and the selection as an artifact
    (started,) = runs.started
    assert started["params"]["prompt_version"] == "3"
    assert started["params"]["llm_model"] == "test-model"
    assert "select_datasets" in started["params"]["tools"]
    assert started["tags"]["folder"] == FOLDER
    (finished,) = runs.finished
    assert finished["message"] == "Only Run3 mc23e"
    assert finished["metrics"]["selection_size"] == 1
    assert MC23E in finished["artifact_text"]

    text = client.get(f"{base}/{created['id']}/datasets.txt").text
    assert [line for line in text.splitlines() if not line.startswith("#")] == [MC23E]

    state = client.get(f"{base}/{created['id']}").json()
    assert [m["role"] for m in state["messages"]] == ["user", "assistant"]


def test_unknown_selection_is_404(agent_client) -> None:
    client, _, _ = agent_client
    assert client.get(f"/api/ewkit/analyses/{FOLDER}/selections/{uuid.uuid4()}").status_code == 404


def test_agent_not_configured_is_503(tmp_path: Path) -> None:
    repository = make_repository()
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        with TestClient(app) as client:
            response = client.post(f"/api/ewkit/analyses/{FOLDER}/selections", json={})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 503
    assert "LLM_BASE_URL" in response.json()["detail"]
