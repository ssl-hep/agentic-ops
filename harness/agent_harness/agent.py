"""The agent under test: a LangChain tool-calling agent on any OpenAI-compatible endpoint."""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI

from agent_harness.config import require


TIMEOUT_SECONDS = 120


def chat_model(model: str, http_client: httpx.AsyncClient | None = None, temperature: float = 0.0) -> ChatOpenAI:
    """Same client the backend uses (backend/app/agents/llm.py), configured from LLM_* in .env."""
    return ChatOpenAI(
        model=model,
        base_url=require("LLM_BASE_URL"),
        api_key=require("LLM_API_KEY"),
        temperature=temperature,
        timeout=TIMEOUT_SECONDS,
        max_retries=1,
        http_async_client=http_client,
    )


def build_agent(
    name: str,
    model: str,
    system_prompt: str,
    tools: Sequence[BaseTool],
    http_client: httpx.AsyncClient | None = None,
):
    return create_agent(chat_model(model, http_client), list(tools), system_prompt=system_prompt, name=name)


def evidence(source: str, body: str) -> str:
    """Wrap supplied content so the model treats it as data, as the backend does (app/agents/tools.py)."""
    body = body.replace("</evidence>", "<\\/evidence>")
    return f'<evidence source="{source}">\n{body}\n</evidence>'


def user_message(question: str, sample_list: str | None = None) -> str:
    """The question, followed by the sample-list excerpt it is about, if any."""
    return f"{question}\n\n{evidence('sample_list', sample_list)}" if sample_list else question


def final_text(result: dict) -> str:
    """The text of the agent's last reply."""
    for message in reversed(result["messages"]):
        if isinstance(message, AIMessage):
            return message.text
    return ""


def make_predict_fn(
    make_agent: Callable[[httpx.AsyncClient], Any], max_steps: int = 10
) -> Callable[[str], Awaitable[str]]:
    """The predict_fn mlflow.genai.evaluate calls with each record's `inputs` as keyword arguments.

    mlflow.genai.evaluate runs an async predict_fn under a fresh asyncio.run() per question, but
    ChatOpenAI shares one cached HTTP connection pool, bound to the event loop that first used it.
    So each question gets its own HTTP client, and an agent built on it, inside the loop running it.
    """

    async def predict_fn(question: str, sample_list: str | None = None) -> str:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            # Each tool call is two graph steps (model, then tool), plus the final answer
            result = await make_agent(client).ainvoke(
                {"messages": [{"role": "user", "content": user_message(question, sample_list)}]},
                config={"recursion_limit": 2 * max_steps + 1},
            )
        return final_text(result)

    return predict_fn
