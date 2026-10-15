"""The agent under test: a LangChain tool-calling agent on any OpenAI-compatible endpoint."""

from collections.abc import Awaitable, Callable, Sequence

from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI

from agent_harness.config import require


def chat_model(model: str, temperature: float = 0.0) -> ChatOpenAI:
    """Same client the backend uses (backend/app/agents/llm.py), configured from LLM_* in .env."""
    return ChatOpenAI(
        model=model,
        base_url=require("LLM_BASE_URL"),
        api_key=require("LLM_API_KEY"),
        temperature=temperature,
        timeout=120,
        max_retries=1,
    )


def build_agent(model: str, system_prompt: str, tools: Sequence[BaseTool]):
    return create_agent(chat_model(model), list(tools), system_prompt=system_prompt, name="fs-explorer")


def final_text(result: dict) -> str:
    """The text of the agent's last reply."""
    for message in reversed(result["messages"]):
        if isinstance(message, AIMessage):
            return message.text
    return ""


def make_predict_fn(agent, max_steps: int = 10) -> Callable[[str], Awaitable[str]]:
    """The predict_fn mlflow.genai.evaluate calls with each record's `inputs` as keyword arguments."""

    async def predict_fn(question: str) -> str:
        # Each tool call is two graph steps (model, then tool), plus the final answer
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": question}]},
            config={"recursion_limit": 2 * max_steps + 1},
        )
        return final_text(result)

    return predict_fn
