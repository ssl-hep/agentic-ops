"""Deterministic checks of a RunSpec at a pinned commit.

Every check here is code. The run setup agents read these findings and explain them, but no
model decides whether a check passes. Checks that need Rucio, the image or HTCondor come later.
"""

import asyncio
import difflib
import re
from collections import Counter, defaultdict

from app import ewkit
from app.repository import EWKitRepository, NotFoundError
from app.runspec import Evidence, Finding, RunSpec, SelectionRow, ValidationReport
from app.schemas import AnalysisSummary, ConfigInfo, ResolvedRef

MAX_EVIDENCE_LINES = 3
MEMORY_RANGE_MB = (1000, 8000)


async def validate(repo: EWKitRepository, resolved: ResolvedRef, spec: RunSpec) -> ValidationReport:
    """Check a spec against EWKit at resolved.sha. `resolved` must be spec.ref resolved."""
    findings: list[Finding] = []
    ref_kind = await repo.ref_kind(spec.ref)

    if spec.purpose == "production" and ref_kind != "tag":
        findings.append(
            Finding(
                id="REF_POLICY",
                code="REF_POLICY",
                severity="blocking",
                field="ref",
                message=(
                    f"A full production must run from a git tag, but {spec.ref!r} is a {ref_kind}. "
                    "Pick a tag, or set purpose to 'test' for a test-sized run with a test suffix."
                ),
                suggestion={"purpose": "test"},
            )
        )
    if not MEMORY_RANGE_MB[0] <= spec.memory_mb <= MEMORY_RANGE_MB[1]:
        findings.append(
            Finding(
                id="MEMORY_OUT_OF_RANGE",
                code="MEMORY_OUT_OF_RANGE",
                severity="warning",
                field="memory_mb",
                message=(
                    f"{spec.memory_mb} MB per job is outside the usual {MEMORY_RANGE_MB[0]}–{MEMORY_RANGE_MB[1]} MB. "
                    "EWGridRun.py requests 2000 MB; large requests wait longer for a slot."
                ),
            )
        )

    try:
        analysis = await repo.analysis(resolved.sha, spec.analysis)
    except NotFoundError:
        folders = [a.folder for a in await repo.analyses(resolved.sha)]
        findings.append(
            Finding(
                id="ANALYSIS_NOT_FOUND",
                code="ANALYSIS_NOT_FOUND",
                severity="blocking",
                field="analysis",
                message=f"There is no analysis folder data/{spec.analysis} at {resolved.short_sha}."
                + _did_you_mean(spec.analysis, folders),
            )
        )
        return ValidationReport(commit=resolved.sha, ref_kind=ref_kind, findings=findings, selection=[])

    findings += _folder_findings(analysis)
    findings += _config_findings(analysis, spec)

    patterns, pattern_findings = _compile_patterns(spec)
    findings += pattern_findings

    selection, selection_findings = await _selection(repo, resolved, analysis, spec, patterns)
    _suggest_skips(spec, selection_findings)
    findings += selection_findings
    findings += await _image_findings(repo, resolved, ref_kind)

    order = {"blocking": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: order[f.severity])
    return ValidationReport(commit=resolved.sha, ref_kind=ref_kind, findings=findings, selection=selection)


def _folder_findings(analysis: AnalysisSummary) -> list[Finding]:
    findings = []
    if analysis.status == "config_only":
        findings.append(
            Finding(
                id="FOLDER_NOT_RUNNABLE",
                code="FOLDER_NOT_RUNNABLE",
                severity="blocking",
                field="analysis",
                message=f"data/{analysis.folder} has no DAODs/Run*_*.txt sample lists, so there is nothing to run.",
                evidence=[Evidence(path=analysis.path, web_url=analysis.web_url)],
            )
        )
    if analysis.folder not in ewkit.CI_TESTED_FOLDERS:
        findings.append(
            Finding(
                id="NOT_IN_CI",
                code="NOT_IN_CI",
                severity="info",
                field="analysis",
                message=(
                    f"EWKit's CI does not run {analysis.folder} on example files, so the test job is the first "
                    "time this config runs in this image."
                ),
            )
        )
    return findings


def _config_findings(analysis: AnalysisSummary, spec: RunSpec) -> list[Finding]:
    config = next((c for c in analysis.configs if c.name == spec.config), None)
    if config is None:
        return [
            Finding(
                id="CONFIG_NOT_FOUND",
                code="CONFIG_NOT_FOUND",
                severity="blocking",
                field="config",
                message=f"data/{analysis.folder} has no {spec.config}."
                + _did_you_mean(spec.config, [c.name for c in analysis.configs]),
                evidence=[Evidence(path=analysis.path, web_url=analysis.web_url)],
                suggestion=_suggest("config", _closest(spec.config, [c.name for c in analysis.configs])),
            )
        ]

    here = [Evidence(path=config.path, web_url=config.web_url)]
    findings = []
    if config.parse_error:
        findings.append(
            Finding(
                id="CONFIG_PARSE_ERROR",
                code="CONFIG_PARSE_ERROR",
                severity="blocking",
                field="config",
                message=f"{config.name} does not parse: {config.parse_error}",
                evidence=here,
            )
        )
    missing = next((step for step in config.extend_chain if not step.exists), None)
    if missing:
        findings.append(
            Finding(
                id="EXTEND_MISSING",
                code="EXTEND_MISSING",
                severity="blocking",
                field="config",
                message=f"{config.name} extends {missing.path}, which does not exist at this commit.",
                evidence=here,
            )
        )
    elif config.extend_chain:
        chain = " → ".join(step.path for step in config.extend_chain)
        findings.append(
            Finding(
                id="EXTEND_READONLY_IMAGE",
                code="EXTEND_READONLY_IMAGE",
                severity="warning",
                field="config",
                message=(
                    f"{config.name} extends {chain}. EWKit writes the merged config_extended.yaml next to it in "
                    "the installed data/ folder, which fails if the image is mounted read-only; jobs need a "
                    "writable overlay until that changes."
                ),
                evidence=here,
            )
        )
    if config.unavailable_modules:
        findings.append(
            Finding(
                id="CONFIG_NEEDS_UNAVAILABLE_MODULES",
                code="CONFIG_NEEDS_UNAVAILABLE_MODULES",
                severity="blocking",
                field="config",
                message=(
                    f"{config.name} loads {', '.join(config.unavailable_modules)}, which the standard EWKit image "
                    "does not include."
                ),
                evidence=here,
            )
        )
    findings += _systematics_findings(config, spec)
    return findings


def _systematics_findings(config: ConfigInfo, spec: RunSpec) -> list[Finding]:
    here = [Evidence(path=config.path, web_url=config.web_url)]
    if config.run_systematics and spec.systematics == "config-default":
        return [
            Finding(
                id="SYSTEMATICS_DEFAULT_ON",
                code="SYSTEMATICS_DEFAULT_ON",
                severity="info",
                field="systematics",
                message=(
                    f"{config.name} sets runSystematics: True, so systematics run although the spec leaves them at "
                    "the config default. Output names get _SYS only with --doSys (systematics: do)."
                ),
                evidence=here,
            )
        ]
    if config.run_systematics and spec.systematics == "no":
        return [
            Finding(
                id="SYSTEMATICS_OVERRIDDEN",
                code="SYSTEMATICS_OVERRIDDEN",
                severity="info",
                field="systematics",
                message=f"{config.name} turns systematics on; --noSys turns them off for this run.",
                evidence=here,
            )
        ]
    return []


def compile_patterns(
    include: list[str], skip: list[str]
) -> tuple[dict[str, list[re.Pattern[str]]], list[tuple[str, str, str]]]:
    """EWKit's -w (include) and --skip regexes, compiled; invalid ones come back as (field, pattern, error)."""
    compiled: dict[str, list[re.Pattern[str]]] = {"include": [], "skip": []}
    errors = []
    for field, patterns in (("include", include), ("skip", skip)):
        for pattern in patterns:
            try:
                compiled[field].append(re.compile(pattern))
            except re.error as exc:
                errors.append((field, pattern, str(exc)))
    return compiled, errors


def _compile_patterns(spec: RunSpec) -> tuple[dict[str, list[re.Pattern[str]]], list[Finding]]:
    compiled, errors = compile_patterns(spec.include, spec.skip)
    findings = [
        Finding(
            id=f"INVALID_PATTERN:{field}:{pattern}",
            code="INVALID_PATTERN",
            severity="blocking",
            field=field,
            message=f"{field} pattern {pattern!r} is not a valid regular expression: {error}",
        )
        for field, pattern, error in errors
    ]
    return compiled, findings


def is_selected(name: str, patterns: dict[str, list[re.Pattern[str]]]) -> bool:
    if patterns["include"] and not any(p.search(name) for p in patterns["include"]):
        return False
    return not any(p.search(name) for p in patterns["skip"])


async def _selection(
    repo: EWKitRepository,
    resolved: ResolvedRef,
    analysis: AnalysisSummary,
    spec: RunSpec,
    patterns: dict[str, list[re.Pattern[str]]],
) -> tuple[list[SelectionRow], list[Finding]]:
    findings: list[Finding] = []
    if not spec.runs:
        findings.append(_blocking("NO_RUNS", "runs", "No runs are selected. Pick Run2, Run3 or both."))
    for run in spec.runs:
        if run not in analysis.runs:
            finding = _blocking(
                f"RUN_NOT_AVAILABLE:{run}",
                "runs",
                f"data/{analysis.folder} has no {run}_*.txt sample lists. Available: {', '.join(analysis.runs) or 'none'}.",
                code="RUN_NOT_AVAILABLE",
            )
            kept = [r for r in spec.runs if r in analysis.runs]
            finding.suggestion = {"runs": kept} if kept else None
            findings.append(finding)
    available_samples = sorted({s.sample for s in analysis.sample_lists})
    if not spec.sample_lists:
        findings.append(
            _blocking(
                "NO_SAMPLE_LISTS",
                "sample_lists",
                f"No sample lists are selected. Available: {', '.join(available_samples) or 'none'}.",
            )
        )
    for sample in spec.sample_lists:
        if sample not in available_samples:
            finding = _blocking(
                f"SAMPLE_LIST_NOT_FOUND:{sample}",
                "sample_lists",
                f"No sample list named {sample!r} in data/{analysis.folder}/DAODs. "
                f"Available: {', '.join(available_samples) or 'none'}."
                + _did_you_mean(sample, available_samples),
                code="SAMPLE_LIST_NOT_FOUND",
            )
            if match := _closest(sample, available_samples):
                renamed = [match if s == sample else s for s in spec.sample_lists]
                finding.suggestion = {"sample_lists": list(dict.fromkeys(renamed))}
            findings.append(finding)

    by_name = {s.name: s for s in analysis.sample_lists}
    pairs = [
        (run, sample)
        for run in spec.runs
        if run in analysis.runs
        for sample in spec.sample_lists
        if sample in available_samples
    ]
    rows = await asyncio.gather(
        *(_selection_row(repo, resolved, analysis, run, sample, by_name, patterns) for run, sample in pairs)
    )
    selection = []
    for row, row_findings in rows:
        selection.append(row)
        findings += row_findings
    if pairs and sum(r.selected for r in selection) == 0:
        findings.append(
            _blocking(
                "EMPTY_SELECTION",
                "sample_lists",
                "The selected lists and include/skip patterns leave no datasets to run.",
            )
        )
    return selection, findings


async def _selection_row(
    repo: EWKitRepository,
    resolved: ResolvedRef,
    analysis: AnalysisSummary,
    run: str,
    sample: str,
    by_name: dict,
    patterns: dict[str, list[re.Pattern[str]]],
) -> tuple[SelectionRow, list[Finding]]:
    name = f"{run}_{sample}"
    summary = by_name.get(name)
    if summary is None:
        row = SelectionRow(run=run, sample=sample, file=f"{name}.txt", exists=False)
        return row, [
            Finding(
                id=f"SAMPLE_LIST_MISSING_FOR_RUN:{name}",
                code="SAMPLE_LIST_MISSING_FOR_RUN",
                severity="warning",
                field="sample_lists",
                message=f"There is no DAODs/{name}.txt, so {run} has no {sample} datasets in this run.",
            )
        ]

    detail = await repo.sample_list(resolved, analysis.folder, name)
    datasets = [d for g in detail.groups for d in g.datasets]
    selected = [d for d in datasets if is_selected(d.name, patterns)]
    row = SelectionRow(
        run=run,
        sample=sample,
        file=f"{name}.txt",
        exists=True,
        datasets=len(datasets),
        selected=len(selected),
        campaigns=dict(sorted(Counter(d.campaign for d in selected).items(), key=lambda kv: ewkit.campaign_sort_key(kv[0]))),
    )
    findings: list[Finding] = []
    if not datasets:
        findings.append(
            Finding(
                id=f"EMPTY_LIST:{name}",
                code="EMPTY_LIST",
                severity="warning",
                field="sample_lists",
                message=f"DAODs/{name}.txt is empty.",
                evidence=[Evidence(path=summary.path, web_url=summary.web_url)],
            )
        )
        return row, findings

    unknown: dict[str, list[str]] = defaultdict(list)
    unrecognised: list[str] = []
    for d in selected:
        if d.kind == "mc" and d.problem:
            unknown[", ".join(d.rtags) or "none"].append(d.name)
        elif d.kind == "unknown":
            unrecognised.append(d.name)
    if not unknown and not unrecognised:
        return row, findings

    line_numbers = _line_numbers(await repo.read_file(resolved.sha, summary.path))
    for rtags, names in unknown.items():
        message = (
            f"{len(names)} of {len(selected)} selected datasets in {name}.txt have "
            + (f"r-tag {rtags}, which the campaign map does not know" if rtags != "none" else "no r-tag")
            + ", so their outputs cannot be named. EWGridRun.py stops on the same datasets."
        )
        findings.append(
            Finding(
                id=f"UNKNOWN_RTAG:{name}:{rtags}",
                code="UNKNOWN_RTAG",
                severity="blocking",
                field="skip",
                message=message,
                evidence=[_excerpt(summary.path, summary.web_url, names, line_numbers)],
            )
        )
    if unrecognised:
        findings.append(
            Finding(
                id=f"UNRECOGNISED_DATASET:{name}",
                code="UNRECOGNISED_DATASET",
                severity="blocking",
                field="skip",
                message=f"{len(unrecognised)} lines in {name}.txt are not recognised ATLAS dataset names.",
                evidence=[_excerpt(summary.path, summary.web_url, unrecognised, line_numbers)],
            )
        )
    return row, findings


async def _image_findings(repo: EWKitRepository, resolved: ResolvedRef, ref_kind: str) -> list[Finding]:
    if ref_kind == "commit":
        return [
            Finding(
                id="IMAGE_UNCHECKED",
                code="IMAGE_UNCHECKED",
                severity="info",
                field="ref",
                message="EWKit's CI tags images by branch or tag name, so an image for a bare commit cannot be looked up.",
            )
        ]
    image = await repo.image(resolved)
    if image.available is False:
        return [
            Finding(
                id="NO_IMAGE_FOR_REF",
                code="NO_IMAGE_FOR_REF",
                severity="warning",
                field="ref",
                message=(
                    f"EWKit's CI has not pushed an image tagged {image.tag!r} for {resolved.ref}. "
                    "Tag pipelines may not build images at all (open question D3)."
                ),
            )
        ]
    if image.available is None:
        return [
            Finding(
                id="IMAGE_UNCHECKED",
                code="IMAGE_UNCHECKED",
                severity="info",
                field="ref",
                message=f"Could not check the CI image for {resolved.ref}: {image.detail or 'unknown reason'}",
            )
        ]
    return []


def _blocking(id: str, field: str, message: str, code: str | None = None) -> Finding:
    return Finding(id=id, code=code or id, severity="blocking", field=field, message=message)


def _suggest_skips(spec: RunSpec, findings: list[Finding]) -> None:
    """Unknown r-tags: suggest skipping each one, keeping the spec's existing skip patterns."""
    for f in findings:
        if f.code == "UNKNOWN_RTAG":
            rtags = f.id.rsplit(":", 1)[-1]
            if rtags != "none":
                extra = [f"_{tag.strip()}_" for tag in rtags.split(",")]
                f.suggestion = {"skip": list(dict.fromkeys([*spec.skip, *extra]))}


def _suggest(field: str, value: str | None) -> dict | None:
    return {field: value} if value else None


def _closest(value: str, options: list[str]) -> str | None:
    close = difflib.get_close_matches(value, options, n=1, cutoff=0.5)
    if not close:
        close = [o for o in options if o.startswith(value) or value.startswith(o)][:1]
    return close[0] if close else None


def _did_you_mean(value: str, options: list[str]) -> str:
    match = _closest(value, options)
    return f" Did you mean {match!r}?" if match else ""


def _line_numbers(text: str) -> dict[str, int]:
    numbers: dict[str, int] = {}
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip().rstrip("/")
        if line and not line.startswith("#"):
            numbers.setdefault(line.split(":", 1)[-1], i)
    return numbers


def _excerpt(path: str, web_url: str, names: list[str], line_numbers: dict[str, int]) -> Evidence:
    shown = names[:MAX_EVIDENCE_LINES]
    numbers = [line_numbers.get(n, 0) for n in shown]
    url = f"{web_url}#L{numbers[0]}" if numbers and numbers[0] else web_url
    return Evidence(path=path, web_url=url, lines=shown, line_numbers=numbers)
