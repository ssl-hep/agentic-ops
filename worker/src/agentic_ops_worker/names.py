"""Task queue names, workflow IDs and production IDs.

The server's authorizer (temporal-server/authz/claimmapper.go) depends on these shapes: a unit
worker's certificate, CN=ewkit-<production>, may poll only queues matching
^ewkit-<production>-[a-z0-9]+$ and use task tokens only of the workflows production/<production>
and dataset/<production>/... . Production IDs have no "-", so one production's queues never match
another's pattern. Change nothing here without changing the authorizer.
"""

import re

PRODUCTION_ID = re.compile(r"[a-z0-9]{1,40}")
# A Rucio DID, scope:name. ATLAS scopes and names use letters, digits, ".", "-" and "_"; a "/"
# would make workflow IDs ambiguous.
DID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*:[A-Za-z0-9][A-Za-z0-9._-]*")

# Task queues polled by k8s pods and by small HTCondor jobs, shared by all productions
CONTROLLER_QUEUE = "controller"
OPS_QUEUE = "ops"
TRIAGE_QUEUE = "triage"
AF_IO_QUEUE = "af-io"

# Memory classes of unit worker queues, smallest first: a unit that runs out of memory moves to
# the next one. The value is the HTCondor request_memory.
MEMCLASSES = ("4g", "8g", "16g")
MEMCLASS_MB = {"4g": 4096, "8g": 8192, "16g": 16384}

AF_IO_KEEPER_WORKFLOW_ID = "af-io-keeper"


def check_production_id(production_id: str) -> str:
    if not PRODUCTION_ID.fullmatch(production_id):
        raise ValueError(f"production ID {production_id!r} is not [a-z0-9]{{1,40}}")
    return production_id


def check_did(did: str) -> str:
    if not DID.fullmatch(did):
        raise ValueError(f"DID {did!r} is not scope:name")
    return did


def check_memclass(memclass: str) -> str:
    if memclass not in MEMCLASSES:
        raise ValueError(f"memory class {memclass!r} is not one of {', '.join(MEMCLASSES)}")
    return memclass


def unit_queue(production_id: str, memclass: str) -> str:
    """The queue unit workers of one production and memory class poll: ewkit-<production>-<memclass>."""
    return f"ewkit-{check_production_id(production_id)}-{check_memclass(memclass)}"


def unit_worker_name(production_id: str) -> str:
    """The CN of a production's unit worker certificates."""
    return f"ewkit-{check_production_id(production_id)}"


def production_workflow_id(production_id: str) -> str:
    return f"production/{check_production_id(production_id)}"


def dataset_workflow_id(production_id: str, did: str) -> str:
    return f"dataset/{check_production_id(production_id)}/{check_did(did)}"


def pool_keeper_workflow_id(production_id: str) -> str:
    return f"pool-keeper/{check_production_id(production_id)}"


def parse_unit_queue(queue: str) -> tuple[str, str]:
    """(production, memclass) of a unit queue; ValueError for any other queue."""
    m = re.fullmatch(r"ewkit-([a-z0-9]{1,40})-([a-z0-9]+)", queue)
    if not m or m[2] not in MEMCLASSES:
        raise ValueError(f"{queue!r} is not a unit queue")
    return m[1], m[2]


def parse_dataset_workflow_id(workflow_id: str) -> tuple[str, str]:
    """(production, DID) of a dataset workflow ID; ValueError for any other ID."""
    kind, _, rest = workflow_id.partition("/")
    production_id, _, did = rest.partition("/")
    if kind != "dataset":
        raise ValueError(f"{workflow_id!r} is not a dataset workflow ID")
    return check_production_id(production_id), check_did(did)
