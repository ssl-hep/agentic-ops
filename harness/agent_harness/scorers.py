"""Deterministic scorers that read the record's expectations and the agent's trace.

They need no judge model, so they work against any endpoint. `judge_scorers` adds MLflow's
LLM-judged Correctness for when a judge model is available.
"""

import os

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


@scorer
def tool_call_count(trace: Trace) -> int:
    """How many tool calls the agent made, to compare efficiency across prompt versions."""
    return len(_tool_names(trace))


def default_scorers() -> list:
    return [expected_facts_present, used_expected_tools, tool_call_count]


def judge_scorers(model: str) -> list:
    """LLM-judged scorers, using the same OpenAI-compatible endpoint as the agent."""
    # MLflow's judges call the endpoint through the OpenAI client, which reads these
    os.environ.setdefault("OPENAI_BASE_URL", os.environ.get("LLM_BASE_URL", ""))
    os.environ.setdefault("OPENAI_API_KEY", os.environ.get("LLM_API_KEY", ""))
    return [Correctness(model=f"openai:/{model}")]
