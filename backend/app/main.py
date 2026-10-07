from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.agents.checkpoint import open_checkpointer
from app.agents.graph import build_setup_graph
from app.agents.llm import chat_model
from app.api import router as ewkit_router
from app.gitlab import GitLabError, ReadOnlyGitLabClient
from app.repository import EWKitRepository, NotFoundError
from app.settings import get_settings
from app.setup_api import AgentModels
from app.setup_api import router as setup_router


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
    app.state.agent_models = AgentModels(
        builder=chat_model(settings, "builder"), reviewer=chat_model(settings, "reviewer")
    )
    async with open_checkpointer(settings) as checkpointer:
        app.state.setup_graph = build_setup_graph(checkpointer)
        yield
    await client.aclose()


app = FastAPI(title="agentic-ops backend", lifespan=lifespan)
app.include_router(ewkit_router)
app.include_router(setup_router)


@app.exception_handler(NotFoundError)
async def not_found(_: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(GitLabError)
async def gitlab_error(_: Request, exc: GitLabError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": exc.message})


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
