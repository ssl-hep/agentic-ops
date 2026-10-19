"""The run setup graph: draft → validate → review → wait for the person, one thread per setup.

    START → start ─┬─ draft (builder, if there is a request) ─┐
                   └──────────────────────────────────────────┴→ validate (code) → review (reviewer) → await_person
    await_person ── apply / edit ──→ validate
                 ── dismiss only ──→ await_person
                 ── redraft ───────→ draft
                 ── finish ────────→ END (spec ready for the input checks and Gate 1)

The builder and reviewer are separate nodes with separate prompts and no shared conversation. State
holds plain JSON so checkpoints stay readable; dependencies (repository, models) come in through the
run context, never through state.
"""

import operator
from dataclasses import dataclass
from typing import Annotated, Literal, NotRequired, TypedDict

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Checkpointer, Command, interrupt

from app.agents.builder import draft_spec
from app.agents.events import event as _event
from app.agents.events import failed as _failed
from app.agents.llm import AgentError, model_name
from app.agents.prompts import BUILDER, REVIEWER, PromptSource
from app.agents.reviewer import review_spec
from app.gitlab import GitLabError
from app.repository import EWKitRepository, NotFoundError
from app.runspec import Finding, IssueCard, RunSpec, RunSpecPatch, ValidationReport, apply_patch
from app.schemas import ResolvedRef
from app.settings import Settings
from app.validator import validate

SetupStatus = Literal["needs_input", "ready"]


@dataclass
class SetupContext:
    repo: EWKitRepository
    settings: Settings
    prompts: PromptSource
    builder: BaseChatModel | None
    reviewer: BaseChatModel | None


class SetupState(TypedDict):
    request: NotRequired[str | None]
    analysis_hint: NotRequired[str | None]
    ref: NotRequired[str | None]
    resolved: NotRequired[dict | None]
    spec: NotRequired[dict | None]
    draft: NotRequired[dict | None]
    report: NotRequired[dict | None]
    issues: NotRequired[list[dict]]
    errors: NotRequired[list[str]]
    models: NotRequired[dict]
    status: NotRequired[SetupStatus]
    history: Annotated[list[dict], operator.add]


async def start(state: SetupState, runtime: Runtime[SetupContext]) -> dict:
    """Resolve the starting ref, and give a form-first setup a default spec for its folder."""
    repo = runtime.context.repo
    try:
        resolved = await repo.resolve(state.get("ref"))
    except (NotFoundError, GitLabError) as exc:
        return _failed(state, "Resolve ref", exc)
    update: dict = {
        "ref": resolved.ref,
        "resolved": resolved.model_dump(),
        "status": "needs_input",
        "history": [_event("created", ref=resolved.ref, commit=resolved.sha)],
    }
    if not state.get("spec") and not state.get("request") and state.get("analysis_hint"):
        update["spec"] = (await default_spec(repo, resolved, state["analysis_hint"])).model_dump()
    return update


async def default_spec(repo: EWKitRepository, resolved: ResolvedRef, folder: str) -> RunSpec:
    """Every run and list in the folder, its default config; a production only from a tag."""
    spec = RunSpec(analysis=folder, ref=resolved.ref)
    try:
        analysis = await repo.analysis(resolved.sha, folder)
    except NotFoundError:
        return spec
    spec.config = analysis.configs[0].name if analysis.configs else spec.config
    spec.runs = analysis.runs
    spec.sample_lists = sorted({s.sample for s in analysis.sample_lists})
    spec.purpose = "production" if await repo.ref_kind(resolved.ref) == "tag" else "test"
    return spec


def after_start(state: SetupState, runtime: Runtime[SetupContext]) -> str:
    if state.get("resolved") and state.get("request") and not state.get("spec") and runtime.context.builder:
        return "draft"
    return "validate"


async def draft(state: SetupState, runtime: Runtime[SetupContext]) -> dict:
    ctx = runtime.context
    if ctx.builder is None or not state.get("request") or not state.get("resolved"):
        return {}
    resolved = ResolvedRef.model_validate(state["resolved"])
    try:
        system = await ctx.prompts.load(BUILDER)
        result = await draft_spec(
            ctx.builder, system.text, ctx.settings, ctx.repo, resolved, state["request"] or "", state.get("analysis_hint")
        )
    except (AgentError, GitLabError) as exc:
        return _failed(state, "Builder", exc)
    model = {"model": model_name(ctx.settings, "builder"), "prompt_version": system.version}
    return {
        "spec": result.spec.model_dump(),
        "draft": {
            "reasons": {r.field: r.reason for r in result.reasons},
            "assumptions": result.assumptions,
            "questions": result.questions,
            "request": state.get("request"),
            **model,
        },
        "models": {**state.get("models", {}), "builder": model},
        "history": [_event("drafted", **model)],
    }


async def validate_spec(state: SetupState, runtime: Runtime[SetupContext]) -> dict:
    repo = runtime.context.repo
    if not state.get("spec"):
        return {"report": None, "issues": []}
    spec = RunSpec.model_validate(state["spec"])
    resolved = ResolvedRef.model_validate(state["resolved"]) if state.get("resolved") else None
    try:
        if resolved is None or resolved.ref != spec.ref:
            resolved = await repo.resolve(spec.ref)
        report = await validate(repo, resolved, spec)
    except NotFoundError as exc:
        missing = Finding(id="REF_NOT_FOUND", code="REF_NOT_FOUND", severity="blocking", field="ref", message=str(exc))
        report = ValidationReport(commit="", ref_kind="commit", findings=[missing], selection=[])
        return {"report": report.model_dump(), "issues": []}
    except GitLabError as exc:
        return {**_failed(state, "Validator", exc), "report": None, "issues": []}
    return {"resolved": resolved.model_dump(), "report": report.model_dump(), "issues": []}


def after_validate(state: SetupState, runtime: Runtime[SetupContext]) -> str:
    report = state.get("report")
    return "review" if runtime.context.reviewer and report and report.get("commit") else "await_person"


async def review(state: SetupState, runtime: Runtime[SetupContext]) -> dict:
    ctx = runtime.context
    assert ctx.reviewer is not None
    spec = RunSpec.model_validate(state["spec"])
    report = ValidationReport.model_validate(state["report"])
    resolved = ResolvedRef.model_validate(state["resolved"])
    try:
        system = await ctx.prompts.load(REVIEWER)
        cards = await review_spec(
            ctx.reviewer, system.text, ctx.settings, ctx.repo, resolved, state.get("request"), spec, report
        )
    except (AgentError, GitLabError) as exc:
        return _failed(state, "Reviewer", exc)
    model = {"model": model_name(ctx.settings, "reviewer"), "prompt_version": system.version}
    return {
        "issues": [c.model_dump() for c in cards],
        "models": {**state.get("models", {}), "reviewer": model},
        "history": [_event("reviewed", cards=len(cards), **model)],
    }


async def await_person(state: SetupState) -> Command:
    """Pause until the person acts on the spec or the cards. The answer resumes the graph."""
    decision: dict = interrupt({"issues": [c["id"] for c in state.get("issues", [])]})
    action = decision.get("action")
    update: dict = {"errors": []}

    if action == "finish":
        return Command(update={**update, "status": "ready", "history": [_event("finished")]}, goto=END)

    if action == "edit":
        spec = RunSpec.model_validate(decision["spec"])
        return Command(
            update={**update, "spec": spec.model_dump(), "history": [_event("edited", spec=spec.model_dump())]},
            goto="validate",
        )

    if action == "redraft":
        return Command(
            update={**update, "request": decision["request"], "history": [_event("redraft", request=decision["request"])]},
            goto="draft",
        )

    # apply: accept and dismiss cards
    accept, dismiss = set(decision.get("accept", [])), set(decision.get("dismiss", []))
    cards = [IssueCard.model_validate(c) for c in state.get("issues", [])]
    spec = RunSpec.model_validate(state["spec"]) if state.get("spec") else None
    events, remaining, changed = [], [], False
    for card in cards:
        if card.id in accept and card.patch and not card.patch_error and spec is not None:
            spec = apply_patch(spec, RunSpecPatch.model_validate(card.patch))
            changed = True
            events.append(_event("accepted", card=card.id, title=card.title, patch=card.patch))
        elif card.id in dismiss or card.id in accept:
            events.append(_event("dismissed" if card.id in dismiss else "acknowledged", card=card.id, title=card.title))
        else:
            remaining.append(card.model_dump())
    update |= {"issues": remaining, "history": events}
    if changed and spec is not None:
        return Command(update={**update, "spec": spec.model_dump()}, goto="validate")
    return Command(update=update, goto="await_person")


def build_setup_graph(checkpointer: Checkpointer):
    graph = StateGraph(SetupState, context_schema=SetupContext)
    graph.add_node("start", start)
    graph.add_node("draft", draft)
    graph.add_node("validate", validate_spec)
    graph.add_node("review", review)
    graph.add_node("await_person", await_person, destinations=("validate", "draft", "await_person", END))
    graph.add_edge(START, "start")
    graph.add_conditional_edges("start", after_start, ["draft", "validate"])
    graph.add_edge("draft", "validate")
    graph.add_conditional_edges("validate", after_validate, ["review", "await_person"])
    graph.add_edge("review", "await_person")
    return graph.compile(checkpointer=checkpointer)
