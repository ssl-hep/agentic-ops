"""harness: seed MLflow, ask the agent one question, or evaluate it against an MLflow dataset.

Every run records its configuration (model, prompt version, MCP servers and tools) in MLflow, so
changing any of them shows up as a new, comparable run in the experiment.
"""

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import mlflow
from mlflow.genai import datasets

from agent_harness import seed as seeding
from agent_harness.agent import build_agent, make_predict_fn
from agent_harness.config import DEFAULT_EXPERIMENT, ConfigError, configure_mlflow, load_env, load_mcp_connections
from agent_harness.mcp_tools import load_tools
from agent_harness.scorers import default_scorers, judge_scorers

DEFAULT_PROMPT = f"prompts:/{seeding.PROMPT_NAME}@{seeding.PROMPT_ALIAS}"


def _agent_config(args) -> tuple:
    """Load the prompt and MCP tools named by the command-line options, and a factory for the agent."""
    prompt = mlflow.genai.load_prompt(args.prompt, link_to_model=False)
    connections = load_mcp_connections(args.mcp_config, args.server)
    tools = asyncio.run(load_tools(connections, args.tools, args.allow_writes))
    system_prompt = prompt.format(user=args.af_user)
    params = {
        "agent": seeding.PROMPT_NAME,
        "llm_model": args.model,
        "prompt_name": prompt.name,
        "prompt_version": str(prompt.version),
        "prompt_uri": args.prompt,
        "mcp_servers": ",".join(connections),
        "tool_allowlist": ",".join(args.tools),
        "tools": ",".join(sorted(t.name for t in tools)),
        "allow_writes": str(args.allow_writes),
        "max_steps": str(args.max_steps),
    }
    return prompt, tools, lambda client: build_agent(args.model, system_prompt, tools, client), params


def _activate_model(prompt, params: dict) -> str:
    """One MLflow LoggedModel per agent configuration, so traces and results group by version.

    LoggedModel params are immutable, so the name carries a hash of the whole configuration: any
    change (tools, steps, ...) gets a new model, and an unchanged one reuses the existing model.
    """
    # The prompt URI may be an alias; the resolved version is what identifies the configuration
    config = {k: v for k, v in params.items() if k != "prompt_uri"}
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:8]
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{params['agent']}-p{prompt.version}-{params['llm_model']}-{digest}")
    existing = mlflow.search_logged_models(filter_string=f"name = '{name}'", max_results=1, output_format="list")
    model = existing[0] if existing else mlflow.create_external_model(name=name, params=config, model_type="agent")
    model_id = mlflow.set_active_model(model_id=model.model_id).model_id
    # Loading again links this prompt version to the model in the UI
    mlflow.genai.load_prompt(f"prompts:/{prompt.name}/{prompt.version}", model_id=model_id)
    return model_id


def cmd_seed(args) -> None:
    experiment = mlflow.get_experiment_by_name(args.experiment)
    for line in seeding.seed(experiment.experiment_id):
        print(line)


def cmd_ask(args) -> None:
    mlflow.langchain.autolog()
    prompt, tools, make_agent, params = _agent_config(args)
    _activate_model(prompt, params)
    print(f"tools: {params['tools']}", file=sys.stderr)
    answer = asyncio.run(make_predict_fn(make_agent, args.max_steps)(args.question))
    print(answer)
    print(f"\ntrace: {mlflow.get_last_active_trace_id()}", file=sys.stderr)


def _load_dataset(name_or_id: str, experiment: str):
    """An evaluation dataset by ID (d-...) or by name within the experiment."""
    if re.fullmatch(r"d-[0-9a-f]{32}", name_or_id):
        return datasets.get_dataset(dataset_id=name_or_id)
    experiment_id = mlflow.get_experiment_by_name(experiment).experiment_id
    dataset = seeding.find_dataset(name_or_id, experiment_id)
    if dataset is None:
        raise ConfigError(f"No dataset {name_or_id!r} in experiment {experiment!r}; run `harness seed` or pass --dataset")
    return dataset


def cmd_eval(args) -> None:
    # Each worker runs a full agent with MCP calls; keep the gateway and the LLM endpoint unflooded
    os.environ.setdefault("MLFLOW_GENAI_EVAL_MAX_WORKERS", str(args.workers))
    dataset = _load_dataset(args.dataset, args.experiment)
    mlflow.langchain.autolog()
    # evaluate() turns on tracing for every installed library that isn't explicitly disabled. The
    # LangChain spans already cover each LLM call; OpenAI's would add a second, orphaned trace.
    mlflow.openai.autolog(disable=True)
    prompt, tools, make_agent, params = _agent_config(args)
    model_id = _activate_model(prompt, params)
    scorers = default_scorers() + (judge_scorers(args.judge_model or args.model) if args.judge else [])

    run_name = f"{params['agent']}-p{prompt.version}-{args.model}"
    with mlflow.start_run(run_name=run_name) as run:
        mlflow.log_params({**params, "dataset": args.dataset, "dataset_digest": dataset.digest})
        result = mlflow.genai.evaluate(
            data=dataset, predict_fn=make_predict_fn(make_agent, args.max_steps), scorers=scorers, model_id=model_id
        )

    print(f"\nrun {run.info.run_id} ({run_name})")
    for key, value in sorted(result.metrics.items()):
        print(f"  {key}: {value:.3f}" if isinstance(value, float) else f"  {key}: {value}")
    experiment_id = run.info.experiment_id
    print(f"\n{mlflow.get_tracking_uri().rstrip('/')}/#/experiments/{experiment_id}/runs/{run.info.run_id}")


def _agent_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="MLflow prompt URI (default: %(default)s)")
    parser.add_argument(
        "--model", default=os.environ.get("LLM_MODEL"), help="LLM model name (default: LLM_MODEL from .env)"
    )
    parser.add_argument(
        "--tools",
        nargs="+",
        default=["fs_*"],
        metavar="GLOB",
        help="MCP tool name globs the agent may use (default: fs_*)",
    )
    parser.add_argument("--allow-writes", action="store_true", help="also allow tools not marked read-only")
    parser.add_argument("--server", nargs="+", metavar="NAME", help="MCP servers from mcp.json (default: all)")
    parser.add_argument("--mcp-config", type=Path, help="path to mcp.json (default: nearest one upwards)")
    parser.add_argument(
        "--af-user",
        default=os.environ.get("AF_USER") or getpass.getuser(),
        help="fills {{ user }} in the prompt (default: AF_USER or your login name)",
    )
    parser.add_argument("--max-steps", type=int, default=10, help="maximum tool calls per question")


def main(argv: list[str] | None = None) -> None:
    # .env first, so option defaults can come from it
    env_file = Path(os.environ["HARNESS_ENV_FILE"]) if os.environ.get("HARNESS_ENV_FILE") else None
    try:
        load_env(env_file)
    except ConfigError as exc:
        sys.exit(str(exc))

    parser = argparse.ArgumentParser(prog="harness", description=__doc__)
    parser.add_argument("--experiment", default=DEFAULT_EXPERIMENT, help="MLflow experiment (default: %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="register the starter prompt and question dataset").set_defaults(func=cmd_seed)

    ask = sub.add_parser("ask", help="ask the agent one question (traced)")
    ask.add_argument("question")
    _agent_options(ask)
    ask.set_defaults(func=cmd_ask)

    evaluate = sub.add_parser("eval", help="evaluate the agent on an MLflow dataset")
    evaluate.add_argument(
        "--dataset", default=seeding.DATASET_NAME, help="MLflow evaluation dataset name or ID (default: %(default)s)"
    )
    evaluate.add_argument("--judge", action="store_true", help="add MLflow's LLM-judged Correctness scorer")
    evaluate.add_argument("--judge-model", help="model for --judge (default: --model)")
    evaluate.add_argument("--workers", type=int, default=2, help="questions evaluated in parallel")
    _agent_options(evaluate)
    evaluate.set_defaults(func=cmd_eval)

    args = parser.parse_args(argv)
    try:
        if getattr(args, "model", "set") is None:
            raise ConfigError("No model: set LLM_MODEL in .env or pass --model")
        configure_mlflow(args.experiment)
        args.func(args)
    except ConfigError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
