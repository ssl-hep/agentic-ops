from typing import Literal

from pydantic import BaseModel

AnalysisStatus = Literal["runnable", "blocked", "config_only", "invalid"]


class ConnectionStatus(BaseModel):
    gitlab_url: str
    project: str
    token_configured: bool
    reachable: bool
    project_web_url: str | None = None
    default_branch: str | None = None
    detail: str | None = None


class RefOption(BaseModel):
    name: str
    kind: Literal["branch", "tag"]
    commit_sha: str
    committed_date: str | None = None
    protected: bool = False
    default: bool = False


class RefList(BaseModel):
    default_branch: str
    branches: list[RefOption]
    tags: list[RefOption]


class ResolvedRef(BaseModel):
    ref: str
    sha: str
    short_sha: str
    title: str
    author_name: str | None = None
    committed_date: str | None = None
    web_url: str


class ImageStatus(BaseModel):
    """The image EWKit's CI pushed for this ref ($CI_REGISTRY_IMAGE:$CI_COMMIT_REF_SLUG)."""

    tag: str | None
    location: str | None = None
    available: bool | None = None
    digest: str | None = None
    created_at: str | None = None
    detail: str | None = None


class SampleListSummary(BaseModel):
    name: str
    run: str
    sample: str
    path: str
    web_url: str
    datasets: int
    groups: int
    campaigns: dict[str, int]
    problems: int


class ExtendStep(BaseModel):
    path: str
    exists: bool
    web_url: str | None = None


class ConfigInfo(BaseModel):
    name: str
    path: str
    web_url: str
    extend_chain: list[ExtendStep]
    blocks: list[str]
    added_block_modules: list[str]
    # Modules, including ones inherited through Extend, that the standard EWKit image lacks
    unavailable_modules: list[str]
    output_tree: str | None = None
    parse_error: str | None = None


class AnalysisSummary(BaseModel):
    folder: str
    path: str
    web_url: str
    status: AnalysisStatus
    status_reason: str | None = None
    configs: list[ConfigInfo]
    extends: str | None = None
    sample_lists: list[SampleListSummary]
    runs: list[str]
    campaigns: dict[str, int]
    code_owners: list[str]
    ntuple_lists: list[str]
    warnings: list[str]


class AnalysisList(BaseModel):
    ref: ResolvedRef
    image: ImageStatus
    analyses: list[AnalysisSummary]


class AnalysisDetail(BaseModel):
    ref: ResolvedRef
    image: ImageStatus
    analysis: AnalysisSummary


class DatasetRow(BaseModel):
    name: str
    kind: Literal["mc", "data", "unknown"]
    project: str | None = None
    dsid: str | None = None
    physics_short: str | None = None
    format: str | None = None
    etag: str | None = None
    simtag: str | None = None
    rtags: list[str] = []
    ptag: str | None = None
    campaign: str
    problem: str | None = None


class DatasetGroupRows(BaseModel):
    heading: str | None
    datasets: list[DatasetRow]


class SampleListDetail(BaseModel):
    ref: ResolvedRef
    folder: str
    summary: SampleListSummary
    groups: list[DatasetGroupRows]


class ConfigFile(BaseModel):
    ref: ResolvedRef
    folder: str
    config: ConfigInfo
    content: str
