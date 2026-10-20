from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import tracking
from app.agents import mcp_tools
from app.agents.llm import AgentError, chat_model
from app.agents.narrower import Narrower
from app.agents.prompts import MlflowPrompt
from app.api import router as ewkit_router
from app.gitlab import GitLabError, ReadOnlyGitLabClient
from app.repository import EWKitRepository, NotFoundError
from app.selection_api import router as selection_router
from app.selections import SelectionStore
from app.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    client = ReadOnlyGitLabClient(
        settings.gitlab_url,
        settings.gitlab_project,
        settings.gitlab_token.get_secret_value().strip() if settings.gitlab_token else None,
        timeout=settings.gitlab_timeout_seconds,
        max_concurrency=settings.gitlab_max_concurrency,
    )
    app.state.repository = EWKitRepository(client)
    app.state.selections = SelectionStore(settings.scratch_dir)
    app.state.narrower = None
    if (model := chat_model(settings)) is not None:
        runs = tracking.start_tracing(settings)
        app.state.narrower = Narrower(
            settings,
            model,
            MlflowPrompt(settings.mlflow_prompt, configured=tracking.tracking_uri(settings) is not None),
            lambda: mcp_tools.load_tools(settings),
            runs,
        )
    yield
    await client.aclose()


app = FastAPI(title="agentic-ops backend", lifespan=lifespan)
app.include_router(ewkit_router)
app.include_router(selection_router)


@app.exception_handler(NotFoundError)
async def not_found(_: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(GitLabError)
async def gitlab_error(_: Request, exc: GitLabError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": exc.message})


@app.exception_handler(AgentError)
async def agent_error(_: Request, exc: AgentError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
