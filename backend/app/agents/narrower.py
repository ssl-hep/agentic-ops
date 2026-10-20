"""The dataset agent: chats with a person to narrow an analysis's DAODs datasets to the ones a run needs.

Per message it loads the system prompt from MLflow, gives the model the selection's dataset tools
plus the allowlisted read-only Rucio tools from the AF MCP gateway, runs a LangChain tool-calling
agent, and logs the message as one MLflow run with its trace.
"""

import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import mlflow
from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware, ToolErrorMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import BaseTool
from mlflow.entities import SpanType
from mlflow.tracing.constant import TraceMetadataKey

from app.agents.dataset_tools import dataset_tools
from app.agents.llm import AgentError
from app.agents.prompts import PromptSource
from app.schemas import ToolCallInfo
from app.selections import Selection
from app.settings import Settings
from app.tracking import QueryRun, RunLogger

AGENT = "ew-kit-dataset-narrower"
MCP_CACHE_SECONDS = 300.0

McpLoader = Callable[[], Awaitable[tuple[list[BaseTool], str | None]]]


@dataclass(frozen=True)
class Answer:
    reply: str
    tool_calls: list[ToolCallInfo]
    run_id: str | None


async def _tool_error(exc: Exception, request) -> str:
    """A failing tool call (a bad argument, a Rucio error) goes back to the model instead of ending the turn."""
    return f"Tool error ({type(exc).__name__}): {str(exc)[:500]}"


class Narrower:
    def __init__(
        self, settings: Settings, model: BaseChatModel, prompt: PromptSource, load_mcp_tools: McpLoader, runs: RunLogger
    ):
        self.settings = settings
        self.model = model
        self.prompt = prompt
        self.load_mcp_tools = load_mcp_tools
        self.runs = runs
        self._mcp: tuple[float, list[BaseTool], str | None] | None = None

    async def _mcp_tools(self) -> tuple[list[BaseTool], str | None]:
        """The Rucio tools, listed at most every few minutes; a failed listing is retried next message."""
        if self._mcp and self._mcp[2] is None and time.monotonic() - self._mcp[0] < MCP_CACHE_SECONDS:
            return self._mcp[1], self._mcp[2]
        tools, note = await self.load_mcp_tools()
        self._mcp = (time.monotonic(), tools, note)
        return tools, note

    async def answer(self, selection: Selection, content: str) -> Answer:
        prompt = await self.prompt.load(folder=selection.folder, ref=selection.ref.ref, sha=selection.ref.short_sha)
        mcp_tools, mcp_note = await self._mcp_tools()
        tools = dataset_tools(selection) + mcp_tools
        system = prompt.text + (f"\n\nNote from the app: {mcp_note}" if mcp_note else "")

        params = {
            "agent": AGENT,
            "llm_model": self.settings.llm_model or "",
            "llm_base_url": self.settings.llm_base_url or "",
            "llm_temperature": str(self.settings.llm_temperature),
            "prompt_name": prompt.name,
            "prompt_version": prompt.version,
            "tools": ",".join(sorted(t.name for t in tools)),
            "mcp_url": self.settings.mcp_url or "",
            "max_tool_calls": str(self.settings.llm_max_tool_calls),
        }
        turn = sum(1 for role, _ in selection.history if role == "user") + 1
        run = await self.runs.start(
            name=f"{selection.folder}-{selection.id[:8]}-{turn}",
            tags={
                "agent": AGENT,
                "selection_id": selection.id,
                "folder": selection.folder,
                "ewkit_ref": selection.ref.ref,
                "ewkit_sha": selection.ref.sha,
                "turn": str(turn),
            },
            params=params,
        )

        history: list[BaseMessage] = [
            HumanMessage(content=text) if role == "user" else AIMessage(content=text) for role, text in selection.history
        ]
        messages = [*history, HumanMessage(content=content)]
        agent = create_agent(
            self.model,
            tools,
            system_prompt=system,
            middleware=[
                ToolCallLimitMiddleware(run_limit=self.settings.llm_max_tool_calls),
                ToolErrorMiddleware(aon_error=_tool_error),
            ],
            name=AGENT,
        )
        before = len(selection.selected)
        started = time.monotonic()
        try:
            with self._trace(selection, run, content) as set_outputs:
                result = await agent.ainvoke(
                    {"messages": messages},
                    # Each tool round is two graph steps; the limit middleware stops tools well before this
                    config={"recursion_limit": 2 * self.settings.llm_max_tool_calls + 10},
                )
                new = result["messages"][len(messages) :]
                reply = next((m.text for m in reversed(new) if isinstance(m, AIMessage) and m.text.strip()), "")
                set_outputs(reply)
        except Exception as exc:
            error = f"The agent failed: {type(exc).__name__}: {exc}"
            await self.runs.finish(
                run,
                message=content,
                reply=error,
                metrics={"latency_seconds": time.monotonic() - started, "selection_size": len(selection.selected)},
                artifact=selection.path,
                failed=True,
            )
            raise AgentError(error) from exc

        tool_calls = [
            ToolCallInfo(name=call["name"], args=call.get("args") or {})
            for m in new
            if isinstance(m, AIMessage)
            for call in m.tool_calls
        ]
        reply = reply or "(The model returned no text.)"
        selection.history += [("user", content), ("assistant", reply)]
        await self.runs.finish(
            run,
            message=content,
            reply=reply,
            metrics={
                "latency_seconds": time.monotonic() - started,
                "tool_calls": len(tool_calls),
                "rucio_tool_calls": sum(1 for c in tool_calls if c.name.startswith("rucio")),
                "selection_size": len(selection.selected),
                "selection_change": len(selection.selected) - before,
            },
            artifact=selection.path,
        )
        return Answer(reply=reply, tool_calls=tool_calls, run_id=run.run_id if run else None)

    @contextmanager
    def _trace(self, selection: Selection, run: QueryRun | None, content: str) -> Iterator[Callable[[str], None]]:
        """One trace per message, linked to its run, LoggedModel and the selection as a session.

        The autologged LangChain spans nest under this span.
        """
        if not self.runs.tracing:
            yield lambda _: None
            return
        with mlflow.start_span(name=AGENT, span_type=SpanType.AGENT) as span:
            span.set_inputs({"message": content, "selection_id": selection.id, "folder": selection.folder})
            metadata = {}
            if run is not None:
                metadata[TraceMetadataKey.SOURCE_RUN] = run.run_id
                if run.model_id:
                    metadata[TraceMetadataKey.MODEL_ID] = run.model_id
            mlflow.update_current_trace(metadata=metadata, session_id=selection.id)
            yield lambda reply: span.set_outputs({"reply": reply})
