"""Dataset builder agent: picks the sample lists and patterns a request means, using read-only tools."""

import json

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.llm import gather_with_tools, structured
from app.agents.prompts import dataset_builder_system
from app.agents.tools import analysis_digest, evidence, repository_tools
from app.repository import EWKitRepository
from app.run_request import DatasetDraft
from app.schemas import ResolvedRef
from app.settings import Settings


async def draft_datasets(
    model: BaseChatModel,
    settings: Settings,
    repo: EWKitRepository,
    resolved: ResolvedRef,
    request: str,
    analysis: str | None,
) -> DatasetDraft:
    # The folder list is small, so give it up front; tools are for the datasets themselves
    folders = {
        a.folder: {"status": a.status, "sample_lists": {s.name: s.datasets for s in a.sample_lists}}
        for a in await repo.analyses(resolved.sha)
    }
    context = [
        f"EWKit ref: {resolved.ref} (commit {resolved.short_sha}).",
        evidence(
            f"data/@{resolved.short_sha}",
            "Analysis folders and their sample lists (dataset counts):\n" + json.dumps(folders),
        ),
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
        SystemMessage(content=dataset_builder_system()),
        HumanMessage(content="Request from the person:\n" + request.strip() + "\n\n" + "\n\n".join(context)),
    ]
    messages = await gather_with_tools(
        model, repository_tools(repo, resolved), messages, settings.llm_max_tool_calls
    )
    return await structured(
        model,
        DatasetDraft,
        messages,
        settings.llm_structured_output,
        "Return the dataset queries now for this request, with a reason per query, your assumptions and any "
        f"questions. The request was: {request.strip()}",
    )
