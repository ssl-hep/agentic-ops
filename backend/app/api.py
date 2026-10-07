"""REST API for the campaign browser: EWKit analysis folders, configs and sample lists at a ref."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.repository import EWKitRepository
from app.schemas import (
    AnalysisDetail,
    AnalysisList,
    ConfigFile,
    ConnectionStatus,
    RefList,
    SampleListDetail,
)

router = APIRouter(prefix="/api/ewkit", tags=["ewkit"])

RefParam = Annotated[
    str | None,
    Query(description="Branch, tag or commit SHA. Defaults to the project's default branch."),
]


def get_repository(request: Request) -> EWKitRepository:
    return request.app.state.repository


Repository = Annotated[EWKitRepository, Depends(get_repository)]


@router.get("/status")
async def status(repo: Repository) -> ConnectionStatus:
    return await repo.status()


@router.get("/refs")
async def refs(repo: Repository) -> RefList:
    return await repo.refs()


@router.get("/analyses")
async def analyses(repo: Repository, ref: RefParam = None) -> AnalysisList:
    resolved = await repo.resolve(ref)
    image, folders = await asyncio.gather(repo.image(resolved), repo.analyses(resolved.sha))
    return AnalysisList(ref=resolved, image=image, analyses=folders)


@router.get("/analyses/{folder}")
async def analysis(repo: Repository, folder: str, ref: RefParam = None) -> AnalysisDetail:
    resolved = await repo.resolve(ref)
    image, summary = await asyncio.gather(repo.image(resolved), repo.analysis(resolved.sha, folder))
    return AnalysisDetail(ref=resolved, image=image, analysis=summary)


@router.get("/analyses/{folder}/samples/{name}")
async def sample_list(repo: Repository, folder: str, name: str, ref: RefParam = None) -> SampleListDetail:
    return await repo.sample_list(await repo.resolve(ref), folder, name)


@router.get("/analyses/{folder}/configs/{name}")
async def config_file(repo: Repository, folder: str, name: str, ref: RefParam = None) -> ConfigFile:
    return await repo.config_file(await repo.resolve(ref), folder, name)
