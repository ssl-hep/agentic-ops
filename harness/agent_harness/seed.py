"""Bootstrap the first prompt version and question dataset in MLflow.

Safe to re-run: anything that already exists is left alone. After seeding, change prompts and
questions in the MLflow UI (or with the MLflow API) so every change is versioned there.
"""

import mlflow
from mlflow.genai import datasets

PROMPT_NAME = "fs-explorer"
PROMPT_ALIAS = "champion"
DATASET_NAME = "fs-explorer-questions"

PROMPT_TEMPLATE = """\
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

QUESTIONS = [
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


def seed_prompt() -> str:
    existing = mlflow.genai.load_prompt(f"prompts:/{PROMPT_NAME}@latest", allow_missing=True)
    if existing is not None:
        return f"prompt {PROMPT_NAME} exists (latest is v{existing.version}); left alone"
    version = mlflow.genai.register_prompt(
        name=PROMPT_NAME,
        template=PROMPT_TEMPLATE,
        commit_message="Initial filesystem explorer prompt",
        tags={"agent": "fs-explorer"},
    )
    mlflow.genai.set_prompt_alias(PROMPT_NAME, alias=PROMPT_ALIAS, version=version.version)
    return f"registered prompt {PROMPT_NAME} v{version.version} as @{PROMPT_ALIAS}"


def find_dataset(name: str, experiment_id: str):
    """The dataset called `name` in the experiment, or None.

    Searches are scoped to the experiment: servers with permissions enabled reject an unscoped
    dataset search, which is what datasets.get_dataset(name=...) does.
    """
    found = datasets.search_datasets(experiment_ids=[experiment_id], filter_string=f"name = '{name}'")
    return found[0] if found else None


def seed_dataset(experiment_id: str) -> str:
    found = find_dataset(DATASET_NAME, experiment_id)
    if found is not None:
        return f"dataset {DATASET_NAME} exists ({found.dataset_id}); left alone"
    dataset = datasets.create_dataset(name=DATASET_NAME, experiment_id=[experiment_id], tags={"agent": "fs-explorer"})
    dataset.merge_records(QUESTIONS)
    return f"created dataset {DATASET_NAME} ({dataset.dataset_id}) with {len(QUESTIONS)} questions"


def seed(experiment_id: str) -> list[str]:
    return [seed_prompt(), seed_dataset(experiment_id)]
