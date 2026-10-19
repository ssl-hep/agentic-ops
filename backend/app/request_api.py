"""REST API for run requests: one LangGraph thread per request, run to its next interrupt on each call."""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.agents.request_graph import RequestContext, RequestStatus
from app.api import Repository
from app.run_request import DatasetQuery, SelectedDataset, SpecReadyReceipt
from app.schemas import ResolvedRef
from app.agents.prompts import PromptSource
from app.setup_api import AgentModels, SettingsDep, get_agent_models, get_prompts
from app.tracking import graph_run

router = APIRouter(prefix="/api/requests", tags=["run request"])


def get_request_graph(request: Request):
    return request.app.state.request_graph


def get_context(
    repo: Repository,
    settings: SettingsDep,
    models: Annotated[AgentModels, Depends(get_agent_models)],
    prompts: Annotated[PromptSource, Depends(get_prompts)],
):
    return RequestContext(repo=repo, settings=settings, prompts=prompts, builder=models.builder)


Context = Annotated[RequestContext, Depends(get_context)]
Graph = Annotated[Any, Depends(get_request_graph)]


class DatasetDraftInfo(BaseModel):
    queries: list[DatasetQuery] = []
    extra_datasets: list[str] = []
    assumptions: list[str] = []
    questions: list[str] = []
    request: str | None = None
    model: str | None = None
    prompt_version: str | None = None


class RequestView(BaseModel):
    id: str
    status: RequestStatus
    waiting: bool
    request: str | None
    analysis_hint: str | None
    resolved: ResolvedRef | None
    datasets: list[SelectedDataset]
    draft: DatasetDraftInfo | None
    receipt: SpecReadyReceipt | None
    errors: list[str]
    history: list[dict[str, Any]]
    agents_enabled: bool


class CreateRequest(BaseModel):
    ref: str | None = Field(default=None, description="EWKit branch, tag or commit; the default branch otherwise")
    analysis: str | None = Field(default=None, description="Folder the person started from")
    request: str | None = Field(default=None, description="Plain-language request for the builder")


class EditAction(BaseModel):
    action: Literal["edit"]
    datasets: list[str]


class RedraftAction(BaseModel):
    action: Literal["redraft"]
    request: str = Field(min_length=1)


class FinishAction(BaseModel):
    action: Literal["finish"]


RequestAction = Annotated[EditAction | RedraftAction | FinishAction, Field(discriminator="action")]


def _config(request_id: str) -> dict:
    return {"configurable": {"thread_id": request_id}}


async def _view(graph, request_id: str, agents_enabled: bool) -> RequestView:
    snapshot = await graph.aget_state(_config(request_id))
    values = snapshot.values
    if not values:
        raise HTTPException(status_code=404, detail=f"No run request {request_id}")
    return RequestView(
        id=request_id,
        status=values.get("status", "needs_input"),
        waiting=bool(snapshot.next),
        request=values.get("request"),
        analysis_hint=values.get("analysis_hint"),
        resolved=values.get("resolved"),
        datasets=values.get("datasets", []),
        draft=values.get("draft"),
        receipt=values.get("receipt"),
        errors=values.get("errors", []),
        history=values.get("history", []),
        agents_enabled=agents_enabled,
    )


@router.post("")
async def create_request(body: CreateRequest, graph: Graph, context: Context) -> RequestView:
    request_id = uuid.uuid4().hex
    state = {
        "ref": body.ref,
        "analysis_hint": body.analysis,
        "request": body.request.strip() if body.request and body.request.strip() else None,
        "datasets": [],
        "errors": [],
        "history": [],
    }
    with graph_run("run-request", request_id, {"action": "create", "request": state["request"]}):
        await graph.ainvoke(state, _config(request_id), context=context)
    return await _view(graph, request_id, context.builder is not None)


@router.get("/{request_id}")
async def get_request(request_id: str, graph: Graph, context: Context) -> RequestView:
    return await _view(graph, request_id, context.builder is not None)


@router.post("/{request_id}/actions")
async def act(request_id: str, action: RequestAction, graph: Graph, context: Context) -> RequestView:
    view = await _view(graph, request_id, context.builder is not None)
    if not view.waiting:
        raise HTTPException(status_code=409, detail="This run request is finished and can no longer change.")
    if isinstance(action, FinishAction) and not view.datasets:
        raise HTTPException(status_code=409, detail="The dataset list is empty; add datasets before marking it ready.")
    if isinstance(action, RedraftAction) and context.builder is None:
        raise HTTPException(status_code=409, detail="The builder agent is off: no model endpoint is configured.")
    with graph_run("run-request", request_id, action.model_dump()):
        await graph.ainvoke(Command(resume=action.model_dump()), _config(request_id), context=context)
    return await _view(graph, request_id, context.builder is not None)
