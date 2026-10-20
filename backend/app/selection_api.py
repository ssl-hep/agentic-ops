"""REST API for the dataset agent: narrow an analysis's DAODs datasets to a scratch subset by chatting."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app.agents.narrower import Narrower
from app.api import Repository
from app.schemas import ChatMessage, CreateSelection, SelectionReply, SelectionState, SendMessage
from app.selections import Selection, SelectionStore

router = APIRouter(prefix="/api/ewkit/analyses/{folder}/selections", tags=["selections"])

MAX_MESSAGE_CHARS = 4000


def get_store(request: Request) -> SelectionStore:
    return request.app.state.selections


def get_narrower(request: Request) -> Narrower:
    narrower = request.app.state.narrower
    if narrower is None:
        raise HTTPException(
            status_code=503,
            detail="The dataset agent is not configured: set LLM_BASE_URL and LLM_MODEL for the backend.",
        )
    return narrower


Store = Annotated[SelectionStore, Depends(get_store)]
Agent = Annotated[Narrower, Depends(get_narrower)]


def state(selection: Selection) -> SelectionState:
    return SelectionState(
        id=selection.id,
        folder=selection.folder,
        sha=selection.ref.sha,
        short_sha=selection.ref.short_sha,
        ref=selection.ref.ref,
        selection=selection.summary(),
        available=selection.catalog.summarize(),
        messages=[ChatMessage(role=role, content=text) for role, text in selection.history],
    )


@router.post("")
async def create_selection(folder: str, body: CreateSelection, repo: Repository, store: Store, _: Agent) -> SelectionState:
    return state(await store.create(repo, folder, body.ref))


@router.get("/{selection_id}")
async def get_selection(folder: str, selection_id: str, store: Store) -> SelectionState:
    return state(store.get(folder, selection_id))


@router.post("/{selection_id}/messages")
async def send_message(folder: str, selection_id: str, body: SendMessage, store: Store, agent: Agent) -> SelectionReply:
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="The message is empty.")
    if len(content) > MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=422, detail=f"The message is longer than {MAX_MESSAGE_CHARS} characters.")
    selection = store.get(folder, selection_id)
    async with selection.lock:
        answer = await agent.answer(selection, content)
    return SelectionReply(
        reply=answer.reply, selection=selection.summary(), tool_calls=answer.tool_calls, mlflow_run_id=answer.run_id
    )


@router.get("/{selection_id}/datasets.txt")
async def download(folder: str, selection_id: str, store: Store) -> FileResponse:
    selection = store.get(folder, selection_id)
    if not selection.path.is_file():
        selection.save()
    return FileResponse(
        selection.path, media_type="text/plain; charset=utf-8", filename=f"{folder}-{selection_id[:8]}.txt"
    )
