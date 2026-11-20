import re
from pathlib import Path

import pytest

from agentic_ops_worker import names

REPO = Path(__file__).resolve().parents[2]


def test_production_id_matches_authorizer() -> None:
    """The authorizer's own pattern, read from its source, is the one names.py checks."""
    go = (REPO / "temporal-server" / "authz" / "claimmapper.go").read_text()
    authorizer = re.search(r"productionID = regexp\.MustCompile\(`\^(.+)\$`\)", go)
    assert authorizer and authorizer[1] == names.PRODUCTION_ID.pattern


@pytest.mark.parametrize("production_id", ["a", "zee2026a", "x" * 40, "0123"])
def test_valid_production_ids(production_id: str) -> None:
    assert names.check_production_id(production_id) == production_id


@pytest.mark.parametrize("production_id", ["", "x" * 41, "Zee", "zee-2026", "zee_2026", "zee/1", "zee 1"])
def test_invalid_production_ids(production_id: str) -> None:
    with pytest.raises(ValueError):
        names.check_production_id(production_id)


def test_unit_queues() -> None:
    assert [names.unit_queue("zee2026a", m) for m in names.MEMCLASSES] == [
        "ewkit-zee2026a-4g",
        "ewkit-zee2026a-8g",
        "ewkit-zee2026a-16g",
    ]
    assert names.parse_unit_queue("ewkit-zee2026a-8g") == ("zee2026a", "8g")
    assert names.unit_worker_name("zee2026a") == "ewkit-zee2026a"
    with pytest.raises(ValueError):
        names.unit_queue("zee2026a", "2g")
    for queue in ["af-io", "controller", "ewkit-zee2026a-2g", "ewkit-zee-2026a-4g", "ewkit-zee2026a"]:
        with pytest.raises(ValueError):
            names.parse_unit_queue(queue)


def test_queues_of_one_production_never_match_another() -> None:
    """The authorizer lets CN=ewkit-<p> poll ^ewkit-<p>-[a-z0-9]+$ (claimmapper.go workerScope)."""
    productions = ["a", "a1", "ab", "b", "zee", "zee2026a"]
    for p in productions:
        allowed = re.compile(rf"^ewkit-{p}-[a-z0-9]+$")
        for q in productions:
            for memclass in names.MEMCLASSES:
                assert bool(allowed.match(names.unit_queue(q, memclass))) == (p == q)
        assert not allowed.match(names.AF_IO_QUEUE)


def test_workflow_ids() -> None:
    did = "mc23_13p6TeV:mc23_13p6TeV.601189.PhPy8EG_AZNLO_Zee.deriv.DAOD_PHYS.e8514_s4162_r15540_p6266"
    assert names.production_workflow_id("zee2026a") == "production/zee2026a"
    assert names.pool_keeper_workflow_id("zee2026a") == "pool-keeper/zee2026a"
    workflow_id = names.dataset_workflow_id("zee2026a", did)
    assert workflow_id == f"dataset/zee2026a/{did}"
    assert names.parse_dataset_workflow_id(workflow_id) == ("zee2026a", did)


@pytest.mark.parametrize(
    "did", ["no-scope", "user.bengal1:a/b", ":name", "scope:", "scope:name with space", "a:b:c/d", ".x:y"]
)
def test_invalid_dids(did: str) -> None:
    with pytest.raises(ValueError):
        names.dataset_workflow_id("zee2026a", did)


@pytest.mark.parametrize(
    "workflow_id", ["production/zee2026a", "dataset/zee2026a", "dataset/Zee/mc23:x", "dataset/zee/mc23:x/y"]
)
def test_not_dataset_workflow_ids(workflow_id: str) -> None:
    with pytest.raises(ValueError):
        names.parse_dataset_workflow_id(workflow_id)
