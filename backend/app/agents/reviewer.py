"""Reviewer agent: explains the validator's findings as issue cards with evidence and RunSpec patches.

Per D29 it sees the request, the RunSpec and the validator report, never the builder's reasoning.
Code then enforces the rules: cards without evidence are dropped, and every patch is checked against
the schema and dry-run through the validator before the person sees it.
"""

import json
import uuid

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from app.agents.llm import gather_with_tools, structured
from app.agents.tools import analysis_digest, evidence, repository_tools
from app.repository import EWKitRepository, NotFoundError
from app.runspec import (
    Evidence,
    IssueCard,
    PatchEffect,
    ReviewerIssue,
    ReviewerOutput,
    RunSpec,
    RunSpecPatch,
    ValidationReport,
    apply_patch,
)
from app.schemas import ResolvedRef
from app.settings import Settings
from app.validator import validate


async def review_spec(
    model: BaseChatModel,
    system: str,
    settings: Settings,
    repo: EWKitRepository,
    resolved: ResolvedRef,
    request: str | None,
    spec: RunSpec,
    report: ValidationReport,
) -> list[IssueCard]:
    try:
        digest = json.dumps(analysis_digest(await repo.analysis(resolved.sha, spec.analysis)), indent=1)
    except NotFoundError:
        digest = f"No folder data/{spec.analysis} at this commit."
    findings = [f.model_dump(exclude_none=True) for f in report.findings]
    selection = [r.model_dump() for r in report.selection]
    body = "\n\n".join(
        [
            "Request from the person:\n" + (request.strip() if request else "(none: the spec was filled in by hand)"),
            f"Commit: {resolved.sha} (ref {resolved.ref!r}, a {report.ref_kind})",
            "RunSpec:\n" + spec.model_dump_json(indent=1),
            evidence("validator", "Findings:\n" + json.dumps(findings, indent=1)),
            evidence("validator", "Selected sample lists:\n" + json.dumps(selection, indent=1)),
            evidence(f"data/{spec.analysis}@{resolved.short_sha}", digest),
        ]
    )
    messages = [SystemMessage(content=system), HumanMessage(content=body)]
    messages = await gather_with_tools(model, repository_tools(repo, resolved), messages, settings.llm_max_tool_calls)
    blocking = sorted({f.code for f in report.blocking})
    output = await structured(
        model,
        ReviewerOutput,
        messages,
        settings.llm_structured_output,
        "Return the issue cards now, most severe first. "
        + (
            f"Every blocking finding needs a card: {', '.join(blocking)}. "
            if blocking
            else "Return no cards if nothing needs the person's attention. "
        )
        + "Do not raise problems with sample lists or runs the spec does not select.",
    )
    return await check_cards(repo, resolved, spec, report, output.issues)


async def check_cards(
    repo: EWKitRepository,
    resolved: ResolvedRef,
    spec: RunSpec,
    report: ValidationReport,
    issues: list[ReviewerIssue],
) -> list[IssueCard]:
    """Enforce the reviewer rules in code; see the module docstring."""
    by_code: dict[str, list] = {}
    for f in report.findings:
        by_code.setdefault(f.code, []).append(f)
    try:
        owners = (await repo.analysis(resolved.sha, spec.analysis)).code_owners
    except NotFoundError:
        owners = []

    cards = []
    for issue in issues:
        codes = [c for c in dict.fromkeys(issue.finding_codes) if c in by_code]
        cited = []
        for citation in issue.evidence:
            if await repo.path_exists(resolved.sha, citation.path):
                url = await repo.blob_url(resolved.sha, citation.path)
                cited.append(
                    Evidence(
                        path=citation.path,
                        web_url=f"{url}#L{citation.line}" if citation.line else url,
                        lines=[citation.excerpt] if citation.excerpt else [],
                        line_numbers=[citation.line or 0] if citation.excerpt else [],
                    )
                )
        if not codes and not cited:
            continue  # no evidence, no card

        evidence_list = cited + [e for code in codes for f in by_code[code] for e in f.evidence]
        seen: set[tuple] = set()
        unique = []
        for e in evidence_list:
            key = (e.path, tuple(e.lines))
            if key not in seen:
                seen.add(key)
                unique.append(e)

        card = IssueCard(
            id=uuid.uuid4().hex[:8],
            severity=issue.severity,
            title=issue.title,
            explanation=issue.explanation,
            finding_codes=codes,
            evidence=unique,
            if_declined=issue.if_declined,
            owners=owners,
        )
        patch = issue.patch if issue.patch and issue.patch.model_dump(exclude_none=True) else None
        if patch is None:
            # No patch from the model: offer code's mechanical fix if the cited findings have exactly one
            suggestions = {json.dumps(f.suggestion, sort_keys=True) for c in codes for f in by_code[c] if f.suggestion}
            if len(suggestions) == 1:
                patch = RunSpecPatch.model_validate(json.loads(suggestions.pop()))
        if patch is not None:
            await _dry_run(repo, resolved, spec, report, patch, card)
        cards.append(card)

    # The validator decides what blocks; a blocking finding never goes without a card
    covered = {code for c in cards for code in c.finding_codes}
    for f in report.blocking:
        if f.code not in covered:
            card = IssueCard(
                id=uuid.uuid4().hex[:8],
                source="validator",
                severity="blocking",
                title=f.message.split(". ")[0].rstrip(".") + ".",
                explanation=f.message,
                finding_codes=[f.code],
                evidence=f.evidence,
                owners=owners,
            )
            if f.suggestion:
                await _dry_run(repo, resolved, spec, report, RunSpecPatch.model_validate(f.suggestion), card)
            cards.append(card)

    order = {"blocking": 0, "warning": 1, "info": 2}
    cards.sort(key=lambda c: order[c.severity])
    return cards


async def _dry_run(
    repo: EWKitRepository,
    resolved: ResolvedRef,
    spec: RunSpec,
    report: ValidationReport,
    patch: RunSpecPatch,
    card: IssueCard,
) -> None:
    """Attach the patch, check it against the schema and run the validator on the patched spec."""
    card.patch = patch.model_dump(exclude_none=True)
    try:
        patched = apply_patch(spec, patch)
    except ValidationError as exc:
        card.patch_error = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return
    if patched.ref != spec.ref:
        try:
            resolved = await repo.resolve(patched.ref)
        except NotFoundError as exc:
            card.patch_error = str(exc)
            return
    after = await validate(repo, resolved, patched)
    before_ids = {f.id for f in report.findings if f.severity != "info"}
    after_ids = {f.id for f in after.findings if f.severity != "info"}
    card.effect = PatchEffect(clears=sorted(before_ids - after_ids), introduces=sorted(after_ids - before_ids))
