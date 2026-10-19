"""A run request: the datasets an analysis run reads, and how they were chosen.

The builder agent never writes dataset names itself. It picks sample lists and EWKit's include/skip
regexes (DatasetQuery); code expands those against the pinned commit into the concrete list.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.repository import EWKitRepository, NotFoundError
from app.schemas import DatasetRow, ResolvedRef, SampleListDetail
from app.validator import compile_patterns, is_selected


class DatasetQuery(BaseModel):
    folder: str = Field(description="Analysis folder under data/, e.g. RadiativeDecays")
    sample_list: str = Field(description="Sample list file name without .txt, e.g. Run3_bkg")
    include: list[str] = Field(
        default_factory=list,
        description="Regexes like EWKit's -w: keep only datasets matching one of them; empty keeps all",
    )
    skip: list[str] = Field(
        default_factory=list, description="Regexes like EWKit's --skip: drop datasets matching any of them"
    )
    reason: str = Field(description="One line on why the run needs these datasets")


class DatasetDraft(BaseModel):
    """What the builder returns: sample-list queries plus the reasoning the person needs to check them."""

    queries: list[DatasetQuery] = Field(default_factory=list, description="One entry per sample list to read")
    extra_datasets: list[str] = Field(
        default_factory=list, description="Dataset names the request itself gives, exactly as written there"
    )
    assumptions: list[str] = Field(default_factory=list, description="Choices the request did not settle")
    questions: list[str] = Field(
        default_factory=list, description="Questions for the person where the request is ambiguous"
    )


class SelectedDataset(BaseModel):
    name: str
    campaign: str | None = None
    # folder/sample list it came from; None when the person or the request named it directly
    source: str | None = None
    line: int | None = None
    reason: str = ""


class SpecReadyReceipt(BaseModel):
    request_id: str
    datasets: int
    accepted_at: datetime


async def sample_list_rows(
    repo: EWKitRepository, resolved: ResolvedRef, folder: str, sample_list: str
) -> tuple[SampleListDetail, list[tuple[str | None, DatasetRow, int | None]]]:
    """Every dataset in one sample list with its group heading and 1-based line number."""
    detail = await repo.sample_list(resolved, folder, sample_list)
    text = await repo.read_file(resolved.sha, detail.summary.path)
    line_of = {line.strip(): i for i, line in enumerate(text.splitlines(), start=1)}
    return detail, [(g.heading, d, line_of.get(d.name)) for g in detail.groups for d in g.datasets]


async def expand(
    repo: EWKitRepository, resolved: ResolvedRef, draft: DatasetDraft
) -> tuple[list[SelectedDataset], list[str]]:
    """The concrete datasets the draft selects, in query order and without duplicates, plus problems."""
    selected: dict[str, SelectedDataset] = {}
    errors: list[str] = []
    for query in draft.queries:
        where = f"{query.folder}/{query.sample_list}"
        patterns, bad = compile_patterns(query.include, query.skip)
        if bad:
            errors += [f"{where}: {field} pattern {pattern!r} is invalid: {error}" for field, pattern, error in bad]
            continue
        try:
            _, rows = await sample_list_rows(repo, resolved, query.folder, query.sample_list)
        except NotFoundError as exc:
            errors.append(f"{where}: {exc}")
            continue
        kept = [(d, line) for _, d, line in rows if is_selected(d.name, patterns)]
        if not kept:
            errors.append(f"{where}: the include/skip patterns leave no datasets ({len(rows)} in the list)")
        for d, line in kept:
            selected.setdefault(
                d.name, SelectedDataset(name=d.name, campaign=d.campaign, source=where, line=line, reason=query.reason)
            )
    for name in draft.extra_datasets:
        name = name.strip().rstrip("/")
        if name:
            selected.setdefault(name, SelectedDataset(name=name, reason="Named in the request"))
    return list(selected.values()), errors
