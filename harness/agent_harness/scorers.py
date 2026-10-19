"""Deterministic scorers that read the record's expectations and the agent's trace.

They need no judge model, so they work against any endpoint. `judge_scorers` adds MLflow's
LLM-judged Correctness for when a judge model is available.
"""

import os
import re

from mlflow.entities import Feedback, SpanType, Trace
from mlflow.genai.scorers import Correctness, scorer


def _tool_names(trace: Trace) -> list[str]:
    return [span.name for span in trace.search_spans(span_type=SpanType.TOOL)]


@scorer
def expected_facts_present(outputs: str, expectations: dict) -> Feedback | None:
    """Fraction of `expected_facts` that appear verbatim (ignoring case) in the answer."""
    facts = expectations.get("expected_facts") or []
    if not facts:
        return None
    answer = (outputs or "").lower()
    missing = [fact for fact in facts if fact.lower() not in answer]
    return Feedback(
        value=1 - len(missing) / len(facts),
        rationale=f"Missing: {missing}" if missing else "All expected facts present",
    )


@scorer
def used_expected_tools(trace: Trace, expectations: dict) -> Feedback | None:
    """Whether the agent called every tool in `expected_tools`."""
    expected = expectations.get("expected_tools") or []
    if not expected:
        return None
    called = set(_tool_names(trace))
    missing = [tool for tool in expected if tool not in called]
    return Feedback(
        value=not missing,
        rationale=f"Not called: {missing}; called: {sorted(called)}" if missing else f"Called: {sorted(called)}",
    )


def final_datasets(answer: str) -> list[str] | None:
    """The DIDs listed under the answer's last `Datasets:` heading, or None if it has no such section."""
    lines = (answer or "").splitlines()
    starts = [i for i, line in enumerate(lines) if re.fullmatch(r"[#*\s]*datasets[*\s]*:[*\s]*", line, re.IGNORECASE)]
    if not starts:
        return None
    names = []
    for line in lines[starts[-1] + 1 :]:
        item = re.fullmatch(r"\s*[-*]\s+`?([^`\s]+)`?\s*", line)
        if item and item[1].lower() != "none":
            names.append(item[1].rstrip("/"))
    return names


def _unscoped(did: str) -> str:
    """mc23_13p6TeV:mc23_13p6TeV.601589... and mc23_13p6TeV.601589... are the same dataset."""
    return did.split(":", 1)[-1].rstrip("/")


def _listed(outputs: str) -> set[str] | None:
    names = final_datasets(outputs)
    return None if names is None else {_unscoped(n) for n in names}


@scorer
def expected_datasets_listed(outputs: str, expectations: dict) -> Feedback | None:
    """Fraction of `expected_datasets` in the answer's final `Datasets:` list."""
    expected = expectations.get("expected_datasets") or []
    if not expected:
        return None
    listed = _listed(outputs)
    if listed is None:
        return Feedback(value=0.0, rationale="The answer has no final Datasets: section")
    missing = [d for d in expected if _unscoped(d) not in listed]
    return Feedback(
        value=1 - len(missing) / len(expected),
        rationale=f"Missing: {missing}" if missing else "All expected datasets listed",
    )


@scorer
def excluded_datasets_absent(outputs: str, expectations: dict) -> Feedback | None:
    """Whether the final `Datasets:` list leaves out every one of `excluded_datasets`."""
    excluded = expectations.get("excluded_datasets") or []
    if not excluded:
        return None
    listed = _listed(outputs)
    if listed is None:
        return Feedback(value=False, rationale="The answer has no final Datasets: section")
    present = [d for d in excluded if _unscoped(d) in listed]
    return Feedback(value=not present, rationale=f"Listed anyway: {present}" if present else "None listed")


@scorer
def tool_call_count(trace: Trace) -> int:
    """How many tool calls the agent made, to compare efficiency across prompt versions."""
    return len(_tool_names(trace))


def default_scorers() -> list:
    return [
        expected_facts_present,
        used_expected_tools,
        expected_datasets_listed,
        excluded_datasets_absent,
        tool_call_count,
    ]


def judge_scorers(model: str) -> list:
    """LLM-judged scorers, using the same OpenAI-compatible endpoint as the agent."""
    # MLflow's judges call the endpoint through the OpenAI client, which reads these
    os.environ.setdefault("OPENAI_BASE_URL", os.environ.get("LLM_BASE_URL", ""))
    os.environ.setdefault("OPENAI_API_KEY", os.environ.get("LLM_API_KEY", ""))
    return [Correctness(model=f"openai:/{model}")]
