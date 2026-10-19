# Agent evaluation harness

Try agentic queries against MCP servers and track every change in MLflow GenAI:

| What changes            | Where it lives in MLflow                                           |
|-------------------------|--------------------------------------------------------------------|
| System prompt           | Prompt Registry (`fs-explorer`, `run-request`; alias `@champion`)  |
| Questions + expectations| Evaluation datasets (`fs-explorer-questions`, `run-request`)       |
| Model, tools, MCP config| Run params and a LoggedModel per agent configuration               |
| Behaviour               | Traces (every LLM call and MCP tool call) and scorer results       |

The agent is a LangChain `create_agent` tool-calling agent (the same stack as the backend) on the
OpenAI-compatible endpoint in `.env`. MCP tools come from the repo-root `mcp.json` via
`langchain-mcp-adapters`, cut down to an allowlist (`--tools`, default per agent) of tools marked
read-only. The AF gateway also exposes destructive tools, so widen the allowlist deliberately.

## Agents

`--agent` picks the agent under test, which sets the default prompt, dataset and tools:

| Agent | Job | Default tools |
|-------|-----|---------------|
| `fs-explorer` (default) | Answer questions about the user's AF storage | `fs_*`, `rucio_list*`, `rucio_get*` |
| `run-request` | Choose the datasets a run reads, and check them in Rucio (do they exist, are they at MWT2, is a rule keeping them there) | `rucio_list*`, `rucio_get*` |

`run-request` is the harness twin of the app's New run step (see the root README). The gateway
has no DID search, so the agent cannot find datasets in Rucio: its candidates are a sample-list
excerpt in EWKit's format (one name per line, `#` lines start a group), given as the record
input `sample_list` and wrapped in an `<evidence>` block, plus any names in the question. Its
answer ends with a `Datasets:` section of `scope:name` lines, which the dataset scorers read.
Its seeded questions use real DIDs; where each one is replicated (what is at MWT2) is a snapshot
from 2026-10-15, so edit the expectations in MLflow as replicas move. A prompt-injection case (an
instruction hidden in a sample-list heading) is a good next question to add.

## Setup

The harness reads the repo-root `.env` (see `.env.example` for the variable names) and `mcp.json`.

```bash
cd harness
uv sync
uv run harness seed      # registers each agent's prompt and question dataset; re-run to add new agents
```

## Use

```bash
uv run harness ask "What is in /home/bengal1/data/atlas?"
uv run harness eval
uv run harness ask --agent run-request --sample-list Run3_bkg.txt "Only what is already at MWT2"
uv run harness eval --agent run-request --judge
```

`eval` loads the prompt and dataset from MLflow, runs every question through the agent, scores
the answers, and prints the run URL. Options:

- `--prompt prompts:/fs-explorer/2` compares a prompt version (edit prompts in the MLflow UI)
- `--model NAME` changes the LLM
- `--agent run-request` evaluates the run-request agent on its own prompt and dataset
- `--tools 'fs_*' 'rucio_list_*'` changes the tools the agent may call
- `--dataset NAME` points at another dataset
- `--judge` adds MLflow's LLM-judged Correctness scorer, using the same endpoint

Built-in scorers (`agent_harness/scorers.py`) read each record's `expectations`:
`expected_facts` (strings the answer must contain), `expected_tools` (tools the agent must
call), `expected_datasets` (DIDs the final `Datasets:` list must hold) and `excluded_datasets`
(DIDs it must not hold), plus `tool_call_count`. Scorers skip records without their expectation.

## Tests

```bash
uv run pytest
```
