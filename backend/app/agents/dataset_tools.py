"""The dataset agent's own tools: look up an analysis's DAODs datasets and edit the selection.

Every tool works on one selection (one analysis folder at one pinned commit). Lookups filter by
run, sample name, campaign, DSID or a substring, and return counts plus capped rows, since a sample
list can hold thousands of datasets. Selection edits only accept datasets that are in the
analysis's lists, so the scratch file can only ever be a subset of them. Repository text reaches
the model only inside <evidence> blocks.
"""

import json

from langchain_core.tools import BaseTool, StructuredTool

from app import ewkit
from app.selections import Entry, Filters, Selection

MAX_ROWS = 50
MAX_NAMES_SHOWN = 20


def evidence(source: str, body: str) -> str:
    """Wrap repository content so the model treats it as data, not instructions."""
    body = body.replace("</evidence>", "<\\/evidence>")
    return f'<evidence source="{source}">\n{body}\n</evidence>'


def _row(e: Entry, selected: set[str]) -> str:
    mark = "*" if e.name in selected else " "
    group = e.sample_list + (f" / {e.heading}" if e.heading else "")
    return f"{mark} [{group}] {e.campaign} {e.name}"


def _summary_json(selection: Selection, names: set[str] | None) -> str:
    return selection.catalog.summarize(names).model_dump_json(exclude={"path"})


def dataset_tools(selection: Selection) -> list[BaseTool]:
    catalog = selection.catalog
    source = f"data/{selection.folder}/DAODs@{selection.ref.short_sha}"

    def unique(entries: list[Entry]) -> list[Entry]:
        seen: set[str] = set()
        return [e for e in entries if not (e.name in seen or seen.add(e.name))]

    def filters(runs, samples, campaigns, contains, dsids) -> Filters:
        return Filters.of(runs, samples, campaigns, contains, dsids)

    def status() -> str:
        return f"The selection now holds {len(selection.selected)} of {len(catalog.names)} datasets."

    async def summarize_datasets() -> str:
        """Overview of the analysis's DAODs datasets: counts by run, sample name and campaign for all
        datasets and for the current selection, and the sample lists (e.g. Run3_bkg) they come from."""
        lists: dict[str, int] = {}
        for e in unique(catalog.entries):
            lists[e.sample_list] = lists.get(e.sample_list, 0) + 1
        body = (
            f'{{"all": {_summary_json(selection, None)},\n'
            f' "selected": {_summary_json(selection, selection.selected)},\n'
            f' "sample_lists": {json.dumps(lists)}}}'
        )
        return evidence(source, body)

    async def list_datasets(
        runs: list[str] | None = None,
        samples: list[str] | None = None,
        campaigns: list[str] | None = None,
        contains: str = "",
        dsids: list[str] | None = None,
        only_selected: bool = False,
        limit: int = 20,
    ) -> str:
        """List the analysis's datasets by run (e.g. Run2, Run3), sample name (e.g. bkg, signal, or a
        list name like Run3_bkg), campaign (e.g. mc23a, mc23e, data22, unknown), DSID, or a substring
        of the name or group heading. Filters combine with AND; values within one filter with OR.
        Returns the match count and at most 50 rows; '*' marks datasets already selected."""
        found = unique(catalog.find(filters(runs, samples, campaigns, contains, dsids)))
        if only_selected:
            found = [e for e in found if e.name in selection.selected]
        shown = found[: max(1, min(limit, MAX_ROWS))]
        header = f"{len(found)} datasets match; showing {len(shown)}"
        return evidence(source, header + "\n" + "\n".join(_row(e, selection.selected) for e in shown))

    async def select_datasets(
        runs: list[str] | None = None,
        samples: list[str] | None = None,
        campaigns: list[str] | None = None,
        contains: str = "",
        dsids: list[str] | None = None,
    ) -> str:
        """Add every dataset matching the filters (same filters as list_datasets) to the selection."""
        f = filters(runs, samples, campaigns, contains, dsids)
        found = unique(catalog.find(f))
        added = selection.add(e.name for e in found)
        return f"{len(found)} datasets match {f.describe()}; added {added}. {status()}"

    async def deselect_datasets(
        runs: list[str] | None = None,
        samples: list[str] | None = None,
        campaigns: list[str] | None = None,
        contains: str = "",
        dsids: list[str] | None = None,
    ) -> str:
        """Remove every dataset matching the filters (same filters as list_datasets) from the selection."""
        f = filters(runs, samples, campaigns, contains, dsids)
        removed = selection.remove(e.name for e in catalog.find(f))
        return f"Removed {removed} datasets matching {f.describe()}. {status()}"

    async def keep_only(
        runs: list[str] | None = None,
        samples: list[str] | None = None,
        campaigns: list[str] | None = None,
        contains: str = "",
        dsids: list[str] | None = None,
    ) -> str:
        """Narrow the current selection: remove every selected dataset that does NOT match the filters."""
        f = filters(runs, samples, campaigns, contains, dsids)
        keep = {e.name for e in catalog.find(f)}
        removed = selection.remove([n for n in selection.selected if n not in keep])
        return f"Kept datasets matching {f.describe()}; removed {removed}. {status()}"

    def resolve_names(names: list[str]) -> tuple[list[str], list[str]]:
        known, unknown = [], []
        for raw in names:
            name = ewkit.parse_dataset(raw).name  # drops a Rucio scope and trailing '/'
            (known if name in catalog.names else unknown).append(name)
        return known, unknown

    def unknown_note(unknown: list[str]) -> str:
        if not unknown:
            return ""
        shown = ", ".join(unknown[:MAX_NAMES_SHOWN]) + (" ..." if len(unknown) > MAX_NAMES_SHOWN else "")
        return f" {len(unknown)} names are not in this analysis's DAODs lists and were ignored: {shown}."

    async def select_by_name(names: list[str]) -> str:
        """Add datasets by exact name (a Rucio scope prefix is allowed). Only names in the analysis's
        DAODs lists are accepted; use this for picks from Rucio checks."""
        known, unknown = resolve_names(names)
        added = selection.add(known)
        return f"Added {added} datasets.{unknown_note(unknown)} {status()}"

    async def deselect_by_name(names: list[str]) -> str:
        """Remove datasets by exact name (a Rucio scope prefix is allowed), e.g. ones Rucio shows are not
        replicated where they are needed."""
        known, unknown = resolve_names(names)
        removed = selection.remove(known)
        return f"Removed {removed} datasets.{unknown_note(unknown)} {status()}"

    async def show_selection(limit: int = 20) -> str:
        """The current selection: counts by run, sample and campaign, and at most 50 of its datasets."""
        rows = [e for e in unique(catalog.entries) if e.name in selection.selected]
        shown = rows[: max(1, min(limit, MAX_ROWS))]
        body = f"{_summary_json(selection, selection.selected)}\nshowing {len(shown)} of {len(rows)}\n"
        return evidence(source, body + "\n".join(_row(e, selection.selected) for e in shown))

    async def clear_selection() -> str:
        """Empty the selection, to start again."""
        removed = selection.clear()
        return f"Removed {removed} datasets. {status()}"

    return [
        StructuredTool.from_function(coroutine=fn)
        for fn in (
            summarize_datasets,
            list_datasets,
            select_datasets,
            deselect_datasets,
            keep_only,
            select_by_name,
            deselect_by_name,
            show_selection,
            clear_selection,
        )
    ]
