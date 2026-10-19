import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.agents.checkpoint import open_checkpointer
from app.agents.graph import build_setup_graph
from app.agents.llm import chat_model
from app.agents.prompts import MlflowPrompts
from app.agents.request_graph import build_request_graph
from app.api import router as ewkit_router
from app.gitlab import GitLabError, ReadOnlyGitLabClient
from app.ops import SpecReadyService
from app.repository import EWKitRepository, NotFoundError
from app.request_api import router as request_router
from app.settings import get_settings
from app.setup_api import AgentModels
from app.setup_api import router as setup_router
from app.tracking import start_tracing, tracking_uri

# Show the app's own INFO logs (the Spec-is-ready hand-off among them), not every library's
_app_logger = logging.getLogger("app")
if not _app_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
    _app_logger.addHandler(_handler)
    _app_logger.setLevel(logging.INFO)


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
    # Blocking HTTP to the tracking server, once at startup
    await asyncio.to_thread(start_tracing, settings)
    app.state.prompts = MlflowPrompts(configured=tracking_uri(settings) is not None)
    app.state.agent_models = AgentModels(
        builder=chat_model(settings, "builder"), reviewer=chat_model(settings, "reviewer")
    )
    async with open_checkpointer(settings) as checkpointer:
        app.state.setup_graph = build_setup_graph(checkpointer)
        app.state.request_graph = build_request_graph(checkpointer, SpecReadyService())
        yield
    await client.aclose()


app = FastAPI(title="agentic-ops backend", lifespan=lifespan)
app.include_router(ewkit_router)
app.include_router(setup_router)
app.include_router(request_router)


@app.exception_handler(NotFoundError)
async def not_found(_: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(GitLabError)
async def gitlab_error(_: Request, exc: GitLabError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": exc.message})


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
