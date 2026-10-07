"""The model behind the run setup agents: any OpenAI-compatible chat completions endpoint.

The endpoint, model and token come from settings (LLM_BASE_URL, LLM_MODEL, LLM_API_KEY). Swapping
providers (D13) means changing those, not this code.
"""

import json
from collections.abc import Sequence
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from app.settings import Settings

Role = Literal["builder", "reviewer"]


class AgentError(Exception):
    """A model call failed or returned something unusable. The message is safe to show."""


def model_name(settings: Settings, role: Role) -> str | None:
    override = settings.llm_builder_model if role == "builder" else settings.llm_reviewer_model
    return override or settings.llm_model


def chat_model(settings: Settings, role: Role) -> BaseChatModel | None:
    """A chat model for one agent, or None when no endpoint is configured."""
    name = model_name(settings, role)
    if not settings.llm_base_url or not name:
        return None
    return ChatOpenAI(
        model=name,
        base_url=settings.llm_base_url,
        # Servers without auth (a local vLLM, say) still need a non-empty key for the client
        api_key=settings.llm_api_key.get_secret_value().strip() if settings.llm_api_key else "EMPTY",
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_seconds,
        max_retries=1,
    )


async def gather_with_tools(
    model: BaseChatModel, tools: Sequence[BaseTool], messages: list[BaseMessage], max_tool_calls: int
) -> list[BaseMessage]:
    """Let the model call read-only tools until it stops asking or runs out of calls."""
    by_name = {t.name: t for t in tools}
    bound = model.bind_tools(list(tools))
    calls = 0
    while calls < max_tool_calls:
        reply = await _call(bound, messages)
        messages.append(reply)
        if not isinstance(reply, AIMessage) or not reply.tool_calls:
            break
        for call in reply.tool_calls:
            calls += 1
            tool = by_name.get(call["name"])
            if tool is None or calls > max_tool_calls:
                content = f"Tool {call['name']} is not available." if tool is None else "Tool call limit reached."
            else:
                try:
                    content = str(await tool.ainvoke(call["args"]))
                except Exception as exc:  # a bad argument should not end the run
                    content = f"Tool error: {exc}"
            messages.append(ToolMessage(content=content, tool_call_id=call["id"], name=call["name"]))
    return messages


async def structured[T: BaseModel](
    model: BaseChatModel, schema: type[T], messages: list[BaseMessage], method: str, instruction: str
) -> T:
    """Ask for the final answer as `schema`, so the model cannot answer in free text."""
    if method == "json_mode":  # JSON mode does not send the schema, so the prompt must
        instruction += f"\nReply with one JSON object matching this JSON Schema:\n{json.dumps(schema.model_json_schema())}"
    runnable = model.with_structured_output(schema, method=method, include_raw=True)
    error = None
    # Some servers ignore a named tool_choice and let the model call another tool, or answer in
    # text; one retry with a firmer instruction usually fixes that
    for prompt in (instruction, f"{instruction}\nCall the {schema.__name__} function now. No other function is available."):
        result = await _call(runnable, [*messages, HumanMessage(content=prompt)])
        parsed = result.get("parsed") if isinstance(result, dict) else None
        if parsed is not None:
            return parsed if isinstance(parsed, schema) else schema.model_validate(parsed)
        error = result.get("parsing_error") if isinstance(result, dict) else None
    raise AgentError(f"The model did not return a valid {schema.__name__}: {error or 'empty answer'}")


async def _call(runnable, messages: list[BaseMessage]):
    try:
        return await runnable.ainvoke(messages)
    except AgentError:
        raise
    except Exception as exc:
        raise AgentError(f"Model call failed: {type(exc).__name__}: {exc}") from exc
