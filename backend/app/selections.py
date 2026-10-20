"""Dataset selections: an analysis's DAODs datasets at a commit, and the subset a person narrows them to.

A selection only ever holds names from the analysis's own DAODs/Run*_*.txt lists, so the scratch
file it writes is always a subset of them. The file is written in EWKit's sample-list format
(one name per line, '#' lines start a group) to <scratch_dir>/<folder>/<id>.txt after every change.

Selections live in memory (one backend replica); the scratch file is the durable result.
"""

import asyncio
import os
import re
import tempfile
import uuid
from collections import Counter, OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app import ewkit
from app.repository import EWKitRepository, NotFoundError
from app.schemas import ResolvedRef, SelectionSummary

MAX_SELECTIONS = 200
_FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class Entry:
    """One dataset line in one sample list."""

    name: str
    run: str
    sample: str
    sample_list: str
    heading: str | None
    campaign: str
    dsid: str | None
    physics_short: str | None


@dataclass(frozen=True)
class Filters:
    """Dataset filters the agent's tools take. Empty fields match everything; matching ignores case."""

    runs: tuple[str, ...] = ()
    samples: tuple[str, ...] = ()
    campaigns: tuple[str, ...] = ()
    contains: str = ""
    dsids: tuple[str, ...] = ()

    @classmethod
    def of(
        cls,
        runs: Iterable[str] | None = None,
        samples: Iterable[str] | None = None,
        campaigns: Iterable[str] | None = None,
        contains: str | None = None,
        dsids: Iterable[str] | None = None,
    ) -> "Filters":
        def norm(values: Iterable[str] | None) -> tuple[str, ...]:
            return tuple(v.strip().lower() for v in values or () if v and v.strip())

        return cls(norm(runs), norm(samples), norm(campaigns), (contains or "").strip().lower(), norm(dsids))

    def matches(self, e: Entry) -> bool:
        if self.runs and not any(e.run.lower() in (r, f"run{r}") for r in self.runs):
            return False
        # A sample is matched by its name ('bkg') or its list name ('Run3_bkg')
        if self.samples and e.sample.lower() not in self.samples and e.sample_list.lower() not in self.samples:
            return False
        if self.campaigns and e.campaign.lower() not in self.campaigns:
            return False
        if self.contains and self.contains not in e.name.lower() and self.contains not in (e.heading or "").lower():
            return False
        if self.dsids and (e.dsid or "") not in self.dsids:
            return False
        return True

    def describe(self) -> str:
        parts = [
            f"{label}={', '.join(values)}"
            for label, values in (("run", self.runs), ("sample", self.samples), ("campaign", self.campaigns), ("dsid", self.dsids))
            if values
        ]
        if self.contains:
            parts.append(f"contains={self.contains!r}")
        return "; ".join(parts) or "no filter (all datasets)"


@dataclass
class Catalog:
    """Every DAODs dataset of one analysis folder at one commit, in file order."""

    folder: str
    entries: list[Entry]

    def __post_init__(self) -> None:
        self.names = {e.name for e in self.entries}

    def find(self, filters: Filters) -> list[Entry]:
        return [e for e in self.entries if filters.matches(e)]

    def summarize(self, names: Iterable[str] | None = None) -> SelectionSummary:
        """Counts by run, sample and campaign, of `names` or of the whole catalog."""
        wanted = None if names is None else set(names)
        seen: set[str] = set()
        runs: Counter[str] = Counter()
        samples: Counter[str] = Counter()
        campaigns: Counter[str] = Counter()
        for e in self.entries:
            if e.name in seen or (wanted is not None and e.name not in wanted):
                continue
            seen.add(e.name)
            runs[e.run] += 1
            samples[e.sample] += 1
            campaigns[e.campaign] += 1
        return SelectionSummary(
            count=len(seen),
            by_run=dict(sorted(runs.items())),
            by_sample=dict(sorted(samples.items())),
            by_campaign=dict(sorted(campaigns.items(), key=lambda kv: ewkit.campaign_sort_key(kv[0]))),
        )


async def build_catalog(repo: EWKitRepository, resolved: ResolvedRef, folder: str) -> Catalog:
    analysis = await repo.analysis(resolved.sha, folder)
    details = await asyncio.gather(*(repo.sample_list(resolved, folder, s.name) for s in analysis.sample_lists))
    entries = [
        Entry(
            name=d.name,
            run=detail.summary.run,
            sample=detail.summary.sample,
            sample_list=detail.summary.name,
            heading=group.heading,
            campaign=d.campaign,
            dsid=d.dsid,
            physics_short=d.physics_short,
        )
        for detail in details
        for group in detail.groups
        for d in group.datasets
    ]
    return Catalog(folder=folder, entries=entries)


Role = Literal["user", "assistant"]


@dataclass
class Selection:
    id: str
    folder: str
    ref: ResolvedRef
    catalog: Catalog
    path: Path
    selected: set[str] = field(default_factory=set)
    history: list[tuple[Role, str]] = field(default_factory=list)
    # One message at a time per selection
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def add(self, names: Iterable[str]) -> int:
        before = len(self.selected)
        self.selected.update(n for n in names if n in self.catalog.names)
        self.save()
        return len(self.selected) - before

    def remove(self, names: Iterable[str]) -> int:
        before = len(self.selected)
        self.selected.difference_update(names)
        self.save()
        return before - len(self.selected)

    def clear(self) -> int:
        removed = len(self.selected)
        self.selected.clear()
        self.save()
        return removed

    def summary(self) -> SelectionSummary:
        summary = self.catalog.summarize(self.selected)
        summary.path = str(self.path) if self.path.exists() else None
        return summary

    def render(self) -> str:
        """The selection in EWKit's sample-list format, grouped by source list and heading, in file order."""
        lines = [
            f"# Dataset selection {self.id} for data/{self.folder} at {self.ref.ref} ({self.ref.short_sha}): "
            f"{len(self.selected)} datasets"
        ]
        written: set[str] = set()
        group: tuple[str, str | None] | None = None
        for e in self.catalog.entries:
            if e.name not in self.selected or e.name in written:
                continue
            if (e.sample_list, e.heading) != group:
                group = (e.sample_list, e.heading)
                lines.append(f"# {e.sample_list}" + (f": {e.heading}" if e.heading else ""))
            lines.append(e.name)
            written.add(e.name)
        return "\n".join(lines) + "\n"

    def save(self) -> None:
        """Write the scratch file atomically, so a reader never sees half a list."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.id}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(self.render())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


class SelectionStore:
    def __init__(self, scratch_dir: Path):
        self.scratch_dir = scratch_dir
        self._selections: OrderedDict[str, Selection] = OrderedDict()

    def path_for(self, folder: str, selection_id: str) -> Path:
        if not _FOLDER_RE.fullmatch(folder):
            raise NotFoundError(f"No analysis folder data/{folder}")
        try:
            selection_id = str(uuid.UUID(selection_id))
        except ValueError as exc:
            raise NotFoundError(f"No selection {selection_id}") from exc
        root = self.scratch_dir.resolve()
        path = (root / folder / f"{selection_id}.txt").resolve()
        if not path.is_relative_to(root):
            raise NotFoundError(f"No selection {selection_id}")
        return path

    async def create(self, repo: EWKitRepository, folder: str, ref: str | None) -> Selection:
        resolved = await repo.resolve(ref)
        catalog = await build_catalog(repo, resolved, folder)  # NotFoundError for an unknown folder
        selection_id = str(uuid.uuid4())
        selection = Selection(
            id=selection_id, folder=folder, ref=resolved, catalog=catalog, path=self.path_for(folder, selection_id)
        )
        selection.save()
        self._selections[selection_id] = selection
        while len(self._selections) > MAX_SELECTIONS:
            self._selections.popitem(last=False)
        return selection

    def get(self, folder: str, selection_id: str) -> Selection:
        selection = self._selections.get(selection_id)
        if selection is None or selection.folder != folder:
            raise NotFoundError(f"No selection {selection_id} for data/{folder}")
        self._selections.move_to_end(selection_id)
        return selection
