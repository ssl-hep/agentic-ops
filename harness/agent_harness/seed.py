"""Bootstrap each agent's first prompt version and question dataset in MLflow.

Safe to re-run: anything that already exists is left alone. After seeding, change prompts and
questions in the MLflow UI (or with the MLflow API) so every change is versioned there.
"""

from dataclasses import dataclass

import mlflow
from mlflow.genai import datasets

from agent_harness import run_request

PROMPT_ALIAS = "champion"

FS_EXPLORER_PROMPT = """\
You are a research assistant for an ATLAS physicist on the UChicago Analysis Facility (AF).
The user's AF username is {{ user }}.

You have read-only access to the user's two storage areas through the fs_* tools:
- root="home" is the NFS home directory /home/{{ user }}
- root="data" is the Ceph data area /data/{{ user }}

Tool paths are relative to the root. Translate absolute paths before calling a tool, for example
/home/{{ user }}/data/atlas becomes root="home", path="data/atlas". Never guess at directory
contents: list or stat them. Prefer fs_list for browsing and fs_grep for searching file contents.

Answer concisely. Quote file and directory names exactly as the tools return them, and give the
absolute path of anything you mention.
"""

FS_EXPLORER_QUESTIONS = [
    {
        "inputs": {"question": "What datasets are in /home/bengal1/data/atlas?"},
        "expectations": {
            "expected_facts": ["mc20_13TeV.700320.Sh_2211_Zee_maxHTpTV2_BFilter"],
            "expected_tools": ["fs_list"],
        },
    },
    {
        "inputs": {
            "question": "How many files are in the Zee sample under /home/bengal1/data/atlas, and what is their total size?"
        },
        "expectations": {"expected_tools": ["fs_list"]},
    },
    {
        "inputs": {"question": "Which DSID and derivation format are the files in /home/bengal1/data/atlas?"},
        "expectations": {"expected_facts": ["700320", "DAOD_PHYS"], "expected_tools": ["fs_list"]},
    },
]


@dataclass(frozen=True)
class AgentSeed:
    """One agent under test: its prompt, its question dataset and the MCP tools it gets by default."""

    name: str
    template: str
    commit_message: str
    dataset: str
    questions: list[dict]
    tools: tuple[str, ...]


AGENTS = {
    agent.name: agent
    for agent in (
        AgentSeed(
            name="fs-explorer",
            template=FS_EXPLORER_PROMPT,
            commit_message="Initial filesystem explorer prompt",
            dataset="fs-explorer-questions",
            questions=FS_EXPLORER_QUESTIONS,
            tools=("fs_*", "rucio_list*", "rucio_get*"),
        ),
        AgentSeed(
            name="run-request",
            template=run_request.PROMPT_TEMPLATE,
            commit_message="Initial run-request prompt: choose a run's datasets, check them in Rucio",
            dataset="run-request",
            questions=run_request.QUESTIONS,
            tools=("rucio_list*", "rucio_get*"),
        ),
    )
}


def seed_prompt(agent: AgentSeed) -> str:
    existing = mlflow.genai.load_prompt(f"prompts:/{agent.name}@latest", allow_missing=True)
    if existing is not None:
        return f"prompt {agent.name} exists (latest is v{existing.version}); left alone"
    version = mlflow.genai.register_prompt(
        name=agent.name, template=agent.template, commit_message=agent.commit_message, tags={"agent": agent.name}
    )
    mlflow.genai.set_prompt_alias(agent.name, alias=PROMPT_ALIAS, version=version.version)
    return f"registered prompt {agent.name} v{version.version} as @{PROMPT_ALIAS}"


def find_dataset(name: str, experiment_id: str):
    """The dataset called `name` in the experiment, or None.

    Searches are scoped to the experiment: servers with permissions enabled reject an unscoped
    dataset search, which is what datasets.get_dataset(name=...) does.
    """
    found = datasets.search_datasets(experiment_ids=[experiment_id], filter_string=f"name = '{name}'")
    return found[0] if found else None


def seed_dataset(agent: AgentSeed, experiment_id: str) -> str:
    found = find_dataset(agent.dataset, experiment_id)
    if found is not None:
        return f"dataset {agent.dataset} exists ({found.dataset_id}); left alone"
    dataset = datasets.create_dataset(name=agent.dataset, experiment_id=[experiment_id], tags={"agent": agent.name})
    dataset.merge_records(agent.questions)
    return f"created dataset {agent.dataset} ({dataset.dataset_id}) with {len(agent.questions)} questions"


def seed(experiment_id: str) -> list[str]:
    return [line for agent in AGENTS.values() for line in (seed_prompt(agent), seed_dataset(agent, experiment_id))]
