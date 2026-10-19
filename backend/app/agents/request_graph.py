"""The run request graph: settle the datasets a run reads, one thread per request.

    START → start ─┬─ draft (builder, if there is a request) → await_person
                   └─────────────────────────────────────────→ await_person
    await_person ── edit ────→ await_person
                 ── redraft ─→ draft
                 ── finish ──→ submit (Spec is ready, procedural) → END

The builder picks sample lists and patterns; code expands them into dataset names. The Spec-is-ready
service is closed over by the submit node only: it is not in the run context, so no node that calls a
model can reach it.
"""

import operator
from dataclasses import dataclass
from typing import Annotated, Literal, NotRequired, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Checkpointer, Command, interrupt

from app.agents.dataset_builder import draft_datasets
from app.agents.events import event, failed
from app.agents.llm import AgentError, model_name
from app.agents.prompts import DATASETS_PROMPT_VERSION
from app.gitlab import GitLabError
from app.ops import SpecReadyService
from app.repository import EWKitRepository, NotFoundError
from app.run_request import SelectedDataset, expand
from app.schemas import ResolvedRef
from app.settings import Settings

RequestStatus = Literal["needs_input", "ready"]


@dataclass
class RequestContext:
    repo: EWKitRepository
    settings: Settings
    builder: BaseChatModel | None


class RequestState(TypedDict):
    request: NotRequired[str | None]
    analysis_hint: NotRequired[str | None]
    ref: NotRequired[str | None]
    resolved: NotRequired[dict | None]
    datasets: NotRequired[list[dict]]
    draft: NotRequired[dict | None]
    errors: NotRequired[list[str]]
    status: NotRequired[RequestStatus]
    receipt: NotRequired[dict | None]
    history: Annotated[list[dict], operator.add]


async def start(state: RequestState, runtime: Runtime[RequestContext]) -> dict:
    try:
        resolved = await runtime.context.repo.resolve(state.get("ref"))
    except (NotFoundError, GitLabError) as exc:
        return failed(state, "Resolve ref", exc)
    return {
        "ref": resolved.ref,
        "resolved": resolved.model_dump(),
        "status": "needs_input",
        "history": [event("created", ref=resolved.ref, commit=resolved.sha)],
    }


def after_start(state: RequestState, runtime: Runtime[RequestContext]) -> str:
    return "draft" if state.get("resolved") and state.get("request") and runtime.context.builder else "await_person"


async def draft(state: RequestState, runtime: Runtime[RequestContext]) -> dict:
    ctx = runtime.context
    if ctx.builder is None or not state.get("request") or not state.get("resolved"):
        return {}
    resolved = ResolvedRef.model_validate(state["resolved"])
    try:
        result = await draft_datasets(
            ctx.builder, ctx.settings, ctx.repo, resolved, state["request"] or "", state.get("analysis_hint")
        )
        datasets, problems = await expand(ctx.repo, resolved, result)
    except (AgentError, GitLabError) as exc:
        return failed(state, "Builder", exc)
    model = {"model": model_name(ctx.settings, "builder"), "prompt_version": DATASETS_PROMPT_VERSION}
    return {
        "datasets": [d.model_dump() for d in datasets],
        "draft": {
            "queries": [q.model_dump() for q in result.queries],
            "extra_datasets": result.extra_datasets,
            "assumptions": result.assumptions,
            "questions": result.questions,
            "request": state.get("request"),
            **model,
        },
        "errors": [f"Expanding the queries: {p}" for p in problems],
        "history": [event("drafted", datasets=len(datasets), **model)],
    }


async def await_person(state: RequestState) -> Command:
    """Pause until the person edits the list, asks for a redraft, or marks the spec ready."""
    decision: dict = interrupt({"datasets": len(state.get("datasets", []))})
    action = decision.get("action")

    if action == "finish":
        return Command(update={"errors": []}, goto="submit")

    if action == "redraft":
        return Command(
            update={"errors": [], "request": decision["request"], "history": [event("redraft", request=decision["request"])]},
            goto="draft",
        )

    # edit: the person's list, in their order; names already known keep where they came from
    known = {d["name"]: d for d in state.get("datasets", [])}
    names = list(dict.fromkeys(n.strip().rstrip("/") for n in decision.get("datasets", []) if n.strip()))
    datasets = [known.get(n) or SelectedDataset(name=n, reason="Added by you").model_dump() for n in names]
    added = [n for n in names if n not in known]
    removed = [n for n in known if n not in set(names)]
    return Command(
        update={"errors": [], "datasets": datasets, "history": [event("edited", added=added, removed=removed)]},
        goto="await_person",
    )


def build_request_graph(checkpointer: Checkpointer, spec_ready: SpecReadyService):
    async def submit(state: RequestState, config: RunnableConfig) -> dict:
        """Hand the final list to the procedural Spec-is-ready service. Deterministic: no model here."""
        names = [d["name"] for d in state.get("datasets", [])]
        receipt = await spec_ready.spec_ready(config["configurable"]["thread_id"], names)
        return {
            "status": "ready",
            "receipt": receipt.model_dump(mode="json"),
            "history": [event("submitted", datasets=len(names))],
        }

    graph = StateGraph(RequestState, context_schema=RequestContext)
    graph.add_node("start", start)
    graph.add_node("draft", draft)
    graph.add_node("await_person", await_person, destinations=("draft", "await_person", "submit"))
    graph.add_node("submit", submit)
    graph.add_edge(START, "start")
    graph.add_conditional_edges("start", after_start, ["draft", "await_person"])
    graph.add_edge("draft", "await_person")
    graph.add_edge("submit", END)
    return graph.compile(checkpointer=checkpointer)
