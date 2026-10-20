"""EWKit repository connector: reads EWKit through the GitLab API at a specific commit.

Everything read at a commit SHA is cached by that SHA, since a commit's contents never change.
Ref names (branches, tags) and CI image tags move, so those are cached only briefly.
"""

import asyncio
import posixpath
import re
import time
from collections import Counter, OrderedDict
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app import ewkit
from app.gitlab import GitLabError, GitLabNotFound, ReadOnlyGitLabClient
from app.schemas import (
    AnalysisStatus,
    AnalysisSummary,
    ConfigFile,
    ConfigInfo,
    ConnectionStatus,
    DatasetGroupRows,
    DatasetRow,
    ExtendStep,
    ImageStatus,
    RefList,
    RefOption,
    ResolvedRef,
    SampleListDetail,
    SampleListSummary,
)

REF_TTL_SECONDS = 60.0
CODEOWNERS_PATHS = ("CODEOWNERS", ".gitlab/CODEOWNERS", "docs/CODEOWNERS")
MAX_EXTEND_DEPTH = 10


class NotFoundError(Exception):
    pass


class _LRU[K, V]:
    def __init__(self, maxsize: int) -> None:
        self._data: OrderedDict[K, V] = OrderedDict()
        self._maxsize = maxsize

    def get(self, key: K) -> V | None:
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key: K, value: V) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)


class _TTL[K, V]:
    def __init__(self, ttl: float) -> None:
        self._data: dict[K, tuple[float, V]] = {}
        self._ttl = ttl

    def get(self, key: K) -> V | None:
        entry = self._data.get(key)
        if entry is None or entry[0] < time.monotonic():
            return None
        return entry[1]

    def put(self, key: K, value: V) -> None:
        self._data[key] = (time.monotonic() + self._ttl, value)


class EWKitRepository:
    def __init__(self, client: ReadOnlyGitLabClient) -> None:
        self.client = client
        self._project: dict[str, Any] | None = None
        self._files: _LRU[tuple[str, str], str] = _LRU(1024)
        self._trees: _LRU[str, dict[str, str]] = _LRU(32)
        self._analyses: _LRU[str, list[AnalysisSummary]] = _LRU(32)
        self._inflight: dict[str, asyncio.Task[list[AnalysisSummary]]] = {}
        self._refs: _TTL[str, ResolvedRef] = _TTL(REF_TTL_SECONDS)
        self._ref_lists: _TTL[str, RefList] = _TTL(REF_TTL_SECONDS)
        self._images: _TTL[str, ImageStatus] = _TTL(REF_TTL_SECONDS)

    # ----- project and refs -------------------------------------------------------------

    async def project(self) -> dict[str, Any]:
        if self._project is None:
            self._project = await self.client.project_info()
        return self._project

    async def status(self) -> ConnectionStatus:
        status = ConnectionStatus(
            gitlab_url=self.client.base_url,
            project=self.client.project,
            token_configured=self.client.token_configured,
            reachable=False,
        )
        try:
            project = await self.project()
        except GitLabNotFound:
            status.detail = (
                f"Project {self.client.project} was not found. Either the path is wrong or the token "
                "cannot see it."
                if self.client.token_configured
                else "No GitLab token is configured, and the project is not public."
            )
            return status
        except GitLabError as exc:
            status.detail = exc.message
            return status
        status.reachable = True
        status.project_web_url = project["web_url"]
        status.default_branch = project.get("default_branch")
        return status

    async def refs(self) -> RefList:
        if cached := self._ref_lists.get("refs"):
            return cached
        project = await self.project()
        default_branch = project.get("default_branch") or "master"
        branches, tags = await asyncio.gather(self.client.branches(), self.client.tags())
        result = RefList(
            default_branch=default_branch,
            branches=sorted(
                (
                    RefOption(
                        name=b["name"],
                        kind="branch",
                        commit_sha=b["commit"]["id"],
                        committed_date=b["commit"].get("committed_date"),
                        protected=b.get("protected", False),
                        default=b["name"] == default_branch,
                    )
                    for b in branches
                ),
                key=lambda r: (not r.default, -_timestamp(r.committed_date)),
            ),
            tags=[
                RefOption(
                    name=t["name"],
                    kind="tag",
                    commit_sha=t["commit"]["id"],
                    committed_date=t["commit"].get("committed_date"),
                    protected=t.get("protected", False),
                )
                for t in tags
            ],
        )
        self._ref_lists.put("refs", result)
        return result

    async def resolve(self, ref: str | None) -> ResolvedRef:
        if not ref:
            ref = (await self.project()).get("default_branch") or "master"
        if cached := self._refs.get(ref):
            return cached
        try:
            commit = await self.client.commit(ref)
        except GitLabNotFound as exc:
            raise NotFoundError(f"No branch, tag or commit named {ref!r} in {self.client.project}") from exc
        resolved = ResolvedRef(
            ref=ref,
            sha=commit["id"],
            short_sha=commit["short_id"],
            title=commit["title"],
            author_name=commit.get("author_name"),
            committed_date=commit.get("committed_date"),
            web_url=commit["web_url"],
        )
        self._refs.put(ref, resolved)
        return resolved

    async def image(self, ref: ResolvedRef) -> ImageStatus:
        if _is_sha_prefix(ref.ref, ref.sha):
            return ImageStatus(tag=None, detail="CI images are tagged by branch or tag name, not by commit.")
        if cached := self._images.get(ref.ref):
            return cached
        tag = ewkit.ref_slug(ref.ref)
        status = ImageStatus(tag=tag)
        try:
            repos = await self.client.registry_repositories()
            project_path = self.client.project.lower()
            repo = next((r for r in repos if r.get("path", "").lower() == project_path), repos[0] if repos else None)
            if repo is None:
                status.detail = "The project has no container registry repositories."
            else:
                status.location = f"{repo['location']}:{tag}"
                details = await self.client.registry_tag(repo["id"], tag)
                status.available = True
                status.digest = details.get("digest")
                status.created_at = details.get("created_at")
        except GitLabNotFound:
            status.available = False
            status.detail = "CI has not pushed an image for this ref."
        except GitLabError as exc:
            status.detail = f"Could not check the registry: {exc.message}"
        self._images.put(ref.ref, status)
        return status

    # ----- files at a commit -------------------------------------------------------------

    async def _data_tree(self, sha: str) -> dict[str, str]:
        """path -> 'blob' | 'tree' for everything under data/."""
        if (cached := self._trees.get(sha)) is not None:
            return cached
        entries = await self.client.tree("data", sha, recursive=True)
        tree = {e["path"]: e["type"] for e in entries}
        self._trees.put(sha, tree)
        return tree

    async def _read(self, sha: str, path: str) -> str:
        if (cached := self._files.get((sha, path))) is not None:
            return cached
        text = await self.client.raw_file(path, sha)
        self._files.put((sha, path), text)
        return text

    async def _codeowners(self, sha: str) -> list[tuple[str, list[str]]]:
        for path in CODEOWNERS_PATHS:
            try:
                return ewkit.parse_codeowners(await self._read(sha, path))
            except GitLabNotFound:
                continue
        return []

    async def _blob_url(self, sha: str, path: str) -> str:
        return f"{(await self.project())['web_url']}/-/blob/{sha}/{path}"

    async def _tree_url(self, sha: str, path: str) -> str:
        return f"{(await self.project())['web_url']}/-/tree/{sha}/{path}"

    # ----- analyses ----------------------------------------------------------------------

    async def analyses(self, sha: str) -> list[AnalysisSummary]:
        if (cached := self._analyses.get(sha)) is not None:
            return cached
        # Concurrent first requests for the same commit share one build
        task = self._inflight.get(sha)
        if task is None:
            task = asyncio.create_task(self._build_analyses(sha))
            self._inflight[sha] = task
            task.add_done_callback(lambda _: self._inflight.pop(sha, None))
        result = await task
        self._analyses.put(sha, result)
        return result

    async def analysis(self, sha: str, folder: str) -> AnalysisSummary:
        for summary in await self.analyses(sha):
            if summary.folder == folder:
                return summary
        raise NotFoundError(f"No analysis folder data/{folder} at {sha[:8]}")

    async def _build_analyses(self, sha: str) -> list[AnalysisSummary]:
        tree, owners = await asyncio.gather(self._data_tree(sha), self._codeowners(sha))
        folders = sorted(
            (p.split("/", 1)[1] for p, kind in tree.items() if kind == "tree" and p.count("/") == 1),
            key=str.lower,
        )
        return list(
            await asyncio.gather(
                *(self._build_analysis(sha, f, tree, owners) for f in folders if f not in ewkit.NON_ANALYSIS_FOLDERS)
            )
        )

    async def _build_analysis(
        self, sha: str, folder: str, tree: dict[str, str], owner_rules: list[tuple[str, list[str]]]
    ) -> AnalysisSummary:
        base = f"data/{folder}"
        config_paths = sorted(
            _children(tree, base, lambda name: bool(ewkit.CONFIG_FILE_RE.match(name))),
            key=lambda p: (posixpath.basename(p) != "config.yaml", p),
        )
        sample_paths = sorted(
            _children(tree, f"{base}/DAODs", lambda name: ewkit.sample_file_parts(name) is not None),
            key=lambda p: ewkit.sample_file_parts(posixpath.basename(p)) or ("", ""),
        )
        ntuple_lists = sorted(
            posixpath.basename(p) for p in _children(tree, f"{base}/ntuples", lambda n: n.endswith(".txt"))
        )

        configs, sample_lists = await asyncio.gather(
            asyncio.gather(*(self._config_info(sha, p, tree) for p in config_paths)),
            asyncio.gather(*(self._sample_summary(sha, p) for p in sample_paths)),
        )
        configs, sample_lists = list(configs), list(sample_lists)

        campaigns: Counter[str] = Counter()
        for sample_list in sample_lists:
            campaigns.update(sample_list.campaigns)

        warnings = [f"{s.name}.txt is empty" for s in sample_lists if s.datasets == 0]
        warnings += [
            f"{s.name}.txt: {s.problems} dataset name(s) with an unknown campaign" for s in sample_lists if s.problems
        ]
        warnings += [f"{c.name}: {c.parse_error}" for c in configs[1:] if c.parse_error]

        status, reason = _status(configs, sample_lists)
        default = configs[0] if configs else None
        extends = default.extend_chain[0].path if default and default.extend_chain else None

        return AnalysisSummary(
            folder=folder,
            path=base,
            web_url=await self._tree_url(sha, base),
            status=status,
            status_reason=reason,
            configs=configs,
            extends=extends,
            sample_lists=sample_lists,
            runs=sorted({s.run for s in sample_lists}),
            campaigns=dict(sorted(campaigns.items(), key=lambda kv: ewkit.campaign_sort_key(kv[0]))),
            code_owners=ewkit.owners_for_path(owner_rules, base),
            ntuple_lists=ntuple_lists,
            warnings=warnings,
        )

    async def _config_info(self, sha: str, path: str, tree: dict[str, str]) -> ConfigInfo:
        summary = ewkit.summarize_config(await self._read(sha, path))
        modules = list(summary.added_block_modules)
        chain: list[ExtendStep] = []
        parse_error = summary.parse_error

        current_path, current = path, summary
        seen = {path}
        while current.extend and len(chain) < MAX_EXTEND_DEPTH:
            target = ewkit.resolve_extend(current_path, current.extend)
            if target is None:
                chain.append(ExtendStep(path=current.extend, exists=False))
                break
            exists = tree.get(target) == "blob"
            chain.append(ExtendStep(path=target, exists=exists, web_url=await self._blob_url(sha, target)))
            if not exists or target in seen:
                break
            seen.add(target)
            current_path, current = target, ewkit.summarize_config(await self._read(sha, target))
            modules += current.added_block_modules
            if current.parse_error and not parse_error:
                parse_error = f"{target}: {current.parse_error}"

        return ConfigInfo(
            name=posixpath.basename(path),
            path=path,
            web_url=await self._blob_url(sha, path),
            extend_chain=chain,
            blocks=summary.blocks,
            added_block_modules=summary.added_block_modules,
            unavailable_modules=ewkit.unavailable_modules(modules),
            output_tree=summary.output_tree,
            parse_error=parse_error,
        )

    async def _sample_summary(self, sha: str, path: str) -> SampleListSummary:
        groups = ewkit.parse_sample_list(await self._read(sha, path))
        return await self._summarize_groups(sha, path, groups)

    async def _summarize_groups(self, sha: str, path: str, groups: list[ewkit.DatasetGroup]) -> SampleListSummary:
        filename = posixpath.basename(path)
        run, sample = ewkit.sample_file_parts(filename) or ("", "")
        datasets = [d for g in groups for d in g.datasets]
        campaigns = Counter(d.campaign for d in datasets)
        return SampleListSummary(
            name=filename.removesuffix(".txt"),
            run=run,
            sample=sample,
            path=path,
            web_url=await self._blob_url(sha, path),
            datasets=len(datasets),
            groups=sum(1 for g in groups if g.heading),
            campaigns=dict(sorted(campaigns.items(), key=lambda kv: ewkit.campaign_sort_key(kv[0]))),
            problems=sum(1 for d in datasets if d.problem),
        )

    async def sample_list(self, ref: ResolvedRef, folder: str, name: str) -> SampleListDetail:
        summary = await self.analysis(ref.sha, folder)
        match = next((s for s in summary.sample_lists if s.name == name), None)
        if match is None:
            raise NotFoundError(f"No sample list {name}.txt in data/{folder}/DAODs")
        groups = ewkit.parse_sample_list(await self._read(ref.sha, match.path))
        return SampleListDetail(
            ref=ref,
            folder=folder,
            summary=match,
            groups=[
                DatasetGroupRows(
                    heading=g.heading,
                    datasets=[DatasetRow(**{**d.__dict__, "rtags": list(d.rtags)}) for d in g.datasets],
                )
                for g in groups
            ],
        )

    async def config_file(self, ref: ResolvedRef, folder: str, name: str) -> ConfigFile:
        summary = await self.analysis(ref.sha, folder)
        match = next((c for c in summary.configs if c.name == name), None)
        if match is None:
            raise NotFoundError(f"No config {name} in data/{folder}")
        return ConfigFile(ref=ref, folder=folder, config=match, content=await self._read(ref.sha, match.path))


def _children(tree: dict[str, str], directory: str, accept: Callable[[str], bool]) -> list[str]:
    prefix = directory + "/"
    return [
        path
        for path, kind in tree.items()
        if kind == "blob" and path.startswith(prefix) and "/" not in path[len(prefix) :] and accept(path[len(prefix) :])
    ]


def _status(configs: list[ConfigInfo], sample_lists: list[SampleListSummary]) -> tuple[AnalysisStatus, str | None]:
    """Runnable status of a folder, judged on its default config (config.yaml if present)."""
    if not configs:
        return "invalid", "No config*.yaml in the folder"
    default = configs[0]
    if default.parse_error:
        return "invalid", f"{default.name} does not parse: {default.parse_error}"
    broken = next((step for step in default.extend_chain if not step.exists), None)
    if broken:
        return "invalid", f"{default.name} extends {broken.path}, which does not exist"
    if not sample_lists:
        return "config_only", "Config only: no DAODs/Run*_*.txt sample lists"
    if default.unavailable_modules:
        packages = sorted({m.split(".", 1)[0] for m in default.unavailable_modules})
        return "blocked", (
            f"Needs {', '.join(packages)} ({', '.join(default.unavailable_modules)}), "
            "which the standard EWKit image does not include"
        )
    return "runnable", None


def _is_sha_prefix(ref: str, sha: str) -> bool:
    return len(ref) >= 7 and bool(re.fullmatch(r"[0-9a-f]+", ref.lower())) and sha.startswith(ref.lower())


def _timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0

