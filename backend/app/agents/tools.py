"""Read-only repository tools for the run setup agents.

Every tool reads EWKit at the setup's pinned commit through the repository connector, which only
sends GET requests. Repository text reaches the model only inside <evidence> blocks, and whole
sample lists are never returned: the largest has over 3,400 lines.
"""

import json

from langchain_core.tools import BaseTool, StructuredTool

from app.repository import EWKitRepository, NotFoundError
from app.run_request import sample_list_rows
from app.schemas import AnalysisSummary, ResolvedRef
from app.validator import compile_patterns, is_selected

MAX_DATASETS = 50
MAX_CONFIG_CHARS = 20000


def evidence(source: str, body: str) -> str:
    """Wrap repository content so the model treats it as data, not instructions."""
    body = body.replace("</evidence>", "<\\/evidence>")
    return f'<evidence source="{source}">\n{body}\n</evidence>'


def analysis_digest(a: AnalysisSummary) -> dict:
    """Config facts and sample list sizes for one folder, computed by code."""
    return {
        "folder": a.folder,
        "status": a.status,
        "status_reason": a.status_reason,
        "configs": [
            {
                "name": c.name,
                "extends": [s.path for s in c.extend_chain],
                "blocks": c.blocks,
                "output_tree": c.output_tree,
                "runSystematics": c.run_systematics,
                "saveCutBookkeepers": c.save_cutbookkeepers,
                "unavailable_modules": c.unavailable_modules,
                "parse_error": c.parse_error,
            }
            for c in a.configs
        ],
        "runs": a.runs,
        "sample_lists": {s.name: {"datasets": s.datasets, "campaigns": s.campaigns} for s in a.sample_lists},
        "code_owners": a.code_owners,
        "warnings": a.warnings,
    }


def repository_tools(repo: EWKitRepository, resolved: ResolvedRef) -> list[BaseTool]:
    sha, short = resolved.sha, resolved.short_sha

    async def list_refs(contains: str = "") -> str:
        """Branches and tags of EWKit whose name contains the given text (case-insensitive), newest first."""
        refs = await repo.refs()
        needle = contains.lower()
        rows = [f"tag {t.name}" for t in refs.tags if needle in t.name.lower()][:30]
        rows += [f"branch {b.name}" for b in refs.branches if needle in b.name.lower()][:10]
        return evidence("refs", "\n".join(rows) or "no matching refs")

    async def list_analyses() -> str:
        """Every analysis folder at the setup's commit: status, configs, runs and sample list sizes."""
        rows = [
            {
                "folder": a.folder,
                "status": a.status,
                "configs": [c.name for c in a.configs],
                "runs": a.runs,
                "sample_lists": {s.name: s.datasets for s in a.sample_lists},
            }
            for a in await repo.analyses(sha)
        ]
        return evidence(f"data/@{short}", json.dumps(rows, indent=1))

    async def get_analysis(folder: str) -> str:
        """Config facts (Extend chain, runSystematics, output tree), sample lists by campaign, code owners and
        warnings for one analysis folder."""
        try:
            a = await repo.analysis(sha, folder)
        except NotFoundError as exc:
            return str(exc)
        return evidence(f"{a.path}@{short}", json.dumps(analysis_digest(a), indent=1))

    async def find_datasets(
        folder: str,
        sample_list: str,
        campaign: str = "",
        contains: str = "",
        include: list[str] | None = None,
        skip: list[str] | None = None,
        limit: int = 20,
    ) -> str:
        """Datasets in one sample list such as Run3_bkg, filtered by campaign (e.g. mc23e, unknown), by a
        substring of the name, and by EWKit-style include/skip regexes. Returns line numbers, headings and
        at most 50 rows."""
        patterns, bad = compile_patterns(include or [], skip or [])
        if bad:
            return "; ".join(f"{field} pattern {pattern!r} is invalid: {error}" for field, pattern, error in bad)
        try:
            detail, all_rows = await sample_list_rows(repo, resolved, folder, sample_list)
        except NotFoundError as exc:
            return str(exc)
        rows, total = [], 0
        for heading, d, line in all_rows:
            if campaign and d.campaign != campaign:
                continue
            if contains and contains.lower() not in d.name.lower():
                continue
            if not is_selected(d.name, patterns):
                continue
            total += 1
            if len(rows) < min(limit, MAX_DATASETS):
                problem = f"  [{d.problem}]" if d.problem else ""
                rows.append(f"L{line or '?'} [{heading or '-'}] {d.campaign} {d.name}{problem}")
        header = f"{total} of {detail.summary.datasets} datasets match; showing {len(rows)}"
        return evidence(f"{detail.summary.path}@{short}", header + "\n" + "\n".join(rows))

    async def read_config(folder: str, name: str = "config.yaml") -> str:
        """The raw YAML of one config file in an analysis folder."""
        try:
            config = await repo.config_file(resolved, folder, name)
        except NotFoundError as exc:
            return str(exc)
        return evidence(f"{config.config.path}@{short}", config.content[:MAX_CONFIG_CHARS])

    return [
        StructuredTool.from_function(coroutine=fn)
        for fn in (list_refs, list_analyses, get_analysis, find_datasets, read_config)
    ]
