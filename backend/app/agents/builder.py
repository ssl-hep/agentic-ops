"""Builder agent: drafts a RunSpec from a plain-language request, using read-only tools."""

import json

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.llm import gather_with_tools, structured
from app.agents.prompts import builder_system
from app.agents.tools import analysis_digest, evidence, repository_tools
from app.repository import EWKitRepository
from app.runspec import BuilderDraft
from app.schemas import ResolvedRef
from app.settings import Settings


async def draft_spec(
    model: BaseChatModel,
    settings: Settings,
    repo: EWKitRepository,
    resolved: ResolvedRef,
    request: str,
    analysis: str | None,
) -> BuilderDraft:
    # The folder list is small, so give it up front; tools are for details
    folders = {
        a.folder: {"status": a.status, "sample_lists": [s.name for s in a.sample_lists]}
        for a in await repo.analyses(resolved.sha)
    }
    context = [
        f"Ref to use unless the request names another: {resolved.ref} (commit {resolved.short_sha}).",
        evidence(f"data/@{resolved.short_sha}", "Analysis folders and their sample lists:\n" + json.dumps(folders)),
    ]
    if analysis and analysis in folders:
        context.append(f"The person started from the analysis folder {analysis}:")
        context.append(
            evidence(
                f"data/{analysis}@{resolved.short_sha}",
                json.dumps(analysis_digest(await repo.analysis(resolved.sha, analysis))),
            )
        )
    messages = [
        SystemMessage(content=builder_system()),
        HumanMessage(content="Request from the person:\n" + request.strip() + "\n\n" + "\n\n".join(context)),
    ]
    messages = await gather_with_tools(
        model, repository_tools(repo, resolved), messages, settings.llm_max_tool_calls
    )
    draft = await structured(
        model,
        BuilderDraft,
        messages,
        settings.llm_structured_output,
        "Return the RunSpec draft now for this request, with a reason per field you set, your assumptions and "
        f"any questions. The request was: {request.strip()}",
    )
    if not draft.spec.ref.strip():
        draft.spec.ref = resolved.ref
    return draft
