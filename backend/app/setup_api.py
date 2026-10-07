"""REST API for run setup: one LangGraph thread per setup, run to its next interrupt on each call."""

import uuid
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.agents.graph import SetupContext, SetupStatus
from app.agents.llm import model_name
from app.api import Repository
from app.runspec import IssueCard, RunSpec, ValidationReport
from app.schemas import ResolvedRef
from app.settings import Settings, StructuredOutputMethod, get_settings

router = APIRouter(prefix="/api/setups", tags=["run setup"])


class AgentModels(BaseModel):
    """The chat models the run setup graph uses; None when no endpoint is configured."""

    model_config = {"arbitrary_types_allowed": True}

    builder: Any = None
    reviewer: Any = None


def get_agent_models(request: Request) -> AgentModels:
    return request.app.state.agent_models


def get_setup_graph(request: Request):
    return request.app.state.setup_graph


SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_context(repo: Repository, settings: SettingsDep, models: Annotated[AgentModels, Depends(get_agent_models)]):
    return SetupContext(repo=repo, settings=settings, builder=models.builder, reviewer=models.reviewer)


Context = Annotated[SetupContext, Depends(get_context)]
Graph = Annotated[Any, Depends(get_setup_graph)]


class AgentsStatus(BaseModel):
    enabled: bool
    base_url: str | None
    builder_model: str | None
    reviewer_model: str | None
    structured_output: StructuredOutputMethod
    api_key_configured: bool
    reachable: bool | None = None
    detail: str | None = None


class DraftInfo(BaseModel):
    reasons: dict[str, str] = {}
    assumptions: list[str] = []
    questions: list[str] = []
    request: str | None = None
    model: str | None = None
    prompt_version: str | None = None


class SetupView(BaseModel):
    id: str
    status: SetupStatus
    waiting: bool
    request: str | None
    analysis_hint: str | None
    resolved: ResolvedRef | None
    spec: RunSpec | None
    draft: DraftInfo | None
    report: ValidationReport | None
    issues: list[IssueCard]
    errors: list[str]
    models: dict[str, dict[str, Any]]
    history: list[dict[str, Any]]
    agents_enabled: bool
    blocking: int = Field(description="Blocking findings in the current report")


class CreateSetup(BaseModel):
    ref: str | None = Field(default=None, description="Branch, tag or commit; the default branch otherwise")
    analysis: str | None = Field(default=None, description="Folder the person started from")
    request: str | None = Field(default=None, description="Plain-language request for the builder")
    spec: RunSpec | None = Field(default=None, description="A spec filled in by hand; skips the builder")


class ApplyAction(BaseModel):
    action: Literal["apply"]
    accept: list[str] = []
    dismiss: list[str] = []


class EditAction(BaseModel):
    action: Literal["edit"]
    spec: RunSpec


class RedraftAction(BaseModel):
    action: Literal["redraft"]
    request: str = Field(min_length=1)


class FinishAction(BaseModel):
    action: Literal["finish"]


SetupAction = Annotated[ApplyAction | EditAction | RedraftAction | FinishAction, Field(discriminator="action")]


def _config(setup_id: str) -> dict:
    return {"configurable": {"thread_id": setup_id}}


async def _view(graph, setup_id: str, agents_enabled: bool) -> SetupView:
    snapshot = await graph.aget_state(_config(setup_id))
    values = snapshot.values
    if not values:
        raise HTTPException(status_code=404, detail=f"No run setup {setup_id}")
    report = ValidationReport.model_validate(values["report"]) if values.get("report") else None
    return SetupView(
        id=setup_id,
        status=values.get("status", "needs_input"),
        waiting=bool(snapshot.next),
        request=values.get("request"),
        analysis_hint=values.get("analysis_hint"),
        resolved=values.get("resolved"),
        spec=values.get("spec"),
        draft=values.get("draft"),
        report=report,
        issues=values.get("issues", []),
        errors=values.get("errors", []),
        models=values.get("models", {}),
        history=values.get("history", []),
        agents_enabled=agents_enabled,
        blocking=len(report.blocking) if report else 0,
    )


@router.get("/agents")
async def agents_status(
    settings: SettingsDep, check: Annotated[bool, Query(description="Call GET {base_url}/models")] = False
) -> AgentsStatus:
    status = AgentsStatus(
        enabled=settings.agents_enabled,
        base_url=settings.llm_base_url,
        builder_model=model_name(settings, "builder"),
        reviewer_model=model_name(settings, "reviewer"),
        structured_output=settings.llm_structured_output,
        api_key_configured=settings.llm_api_key is not None,
    )
    if check and settings.llm_base_url:
        headers = {"Authorization": f"Bearer {settings.llm_api_key.get_secret_value().strip()}"} if settings.llm_api_key else {}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(settings.llm_base_url.rstrip("/") + "/models", headers=headers)
            status.reachable = response.is_success
            if not response.is_success:
                status.detail = f"GET /models returned {response.status_code}"
        except httpx.HTTPError as exc:
            status.reachable = False
            status.detail = f"Cannot reach {settings.llm_base_url}: {type(exc).__name__}"
    return status


@router.post("")
async def create_setup(body: CreateSetup, graph: Graph, context: Context) -> SetupView:
    setup_id = uuid.uuid4().hex
    state = {
        "ref": body.spec.ref if body.spec else body.ref,
        "analysis_hint": body.spec.analysis if body.spec else body.analysis,
        "request": body.request.strip() if body.request and body.request.strip() else None,
        "spec": body.spec.model_dump() if body.spec else None,
        "issues": [],
        "errors": [],
        "models": {},
        "history": [],
    }
    await graph.ainvoke(state, _config(setup_id), context=context)
    return await _view(graph, setup_id, context.builder is not None)


@router.get("/{setup_id}")
async def get_setup(setup_id: str, graph: Graph, context: Context) -> SetupView:
    return await _view(graph, setup_id, context.builder is not None)


@router.post("/{setup_id}/actions")
async def act(setup_id: str, action: SetupAction, graph: Graph, context: Context) -> SetupView:
    view = await _view(graph, setup_id, context.builder is not None)
    if not view.waiting:
        raise HTTPException(status_code=409, detail="This run setup is finished and can no longer change.")
    if isinstance(action, FinishAction) and (view.spec is None or view.report is None or view.blocking):
        raise HTTPException(
            status_code=409, detail="The spec still has blocking findings; resolve them before marking it ready."
        )
    if isinstance(action, RedraftAction) and context.builder is None:
        raise HTTPException(status_code=409, detail="The builder agent is off: no model endpoint is configured.")
    if isinstance(action, ApplyAction):
        known = {c.id for c in view.issues}
        if unknown := (set(action.accept) | set(action.dismiss)) - known:
            raise HTTPException(status_code=422, detail=f"Unknown issue cards: {', '.join(sorted(unknown))}")
    await graph.ainvoke(Command(resume=action.model_dump()), _config(setup_id), context=context)
    return await _view(graph, setup_id, context.builder is not None)
