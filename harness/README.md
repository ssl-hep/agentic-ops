# Agent evaluation harness

Try agentic queries against MCP servers and track every change in MLflow GenAI:

| What changes            | Where it lives in MLflow                                           |
|-------------------------|--------------------------------------------------------------------|
| System prompt           | Prompt Registry (`fs-explorer`, alias `@champion`)                 |
| Questions + expectations| Evaluation dataset (`fs-explorer-questions`)                       |
| Model, tools, MCP config| Run params and a LoggedModel per agent configuration               |
| Behaviour               | Traces (every LLM call and MCP tool call) and scorer results       |

The agent is a LangChain `create_agent` tool-calling agent (the same stack as the backend) on the
OpenAI-compatible endpoint in `.env`. MCP tools come from the repo-root `mcp.json` via
`langchain-mcp-adapters`, cut down to an allowlist (`--tools`, default `fs_*`) of tools marked
read-only. The AF gateway also exposes destructive tools, so widen the allowlist deliberately.

## Setup

The harness reads the repo-root `.env` (see `.env.example` for the variable names) and `mcp.json`.

```bash
cd harness
uv sync
uv run harness seed      # first time only: registers the prompt and the question dataset
```

## Use

```bash
uv run harness ask "What is in /home/bengal1/data/atlas?"
uv run harness eval
```

`eval` loads the prompt and dataset from MLflow, runs every question through the agent, scores
the answers, and prints the run URL. Options:

- `--prompt prompts:/fs-explorer/2` compares a prompt version (edit prompts in the MLflow UI)
- `--model NAME` changes the LLM
- `--tools 'fs_*' 'rucio_list_*'` changes the tools the agent may call
- `--dataset NAME` points at another dataset
- `--judge` adds MLflow's LLM-judged Correctness scorer, using the same endpoint

Built-in scorers (`agent_harness/scorers.py`) read each record's `expectations`:
`expected_facts` (strings the answer must contain), `expected_tools` (tools the agent must
call), plus `tool_call_count`.

## Tests

```bash
uv run pytest
```
