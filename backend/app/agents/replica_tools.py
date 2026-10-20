"""check_replicas: one agent tool call checks where many datasets have Rucio replicas.

Checking replicas through the gateway's rucio_* tools costs one tool call per dataset, which runs
the agent out of its per-message tool calls, and each answer is a table with a row per child
dataset and RSE (hundreds of rows for one data container). This tool calls the gateway for every
dataset concurrently and returns one line per dataset: the RSEs holding available replicas, with
how many child datasets and bytes each holds.
"""

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from langchain_core.tools import BaseTool, StructuredTool

from app import ewkit
from app.agents.dataset_tools import evidence
from app.selections import Selection

CONTAINER_TOOL = "rucio_list_container_replicas"
DATASET_TOOL = "rucio_list_dataset_replicas"
MAX_DATASETS = 100
MAX_CONCURRENCY = 8
CHECK_TIMEOUT_SECONDS = 90.0
# RSE rows asked for per dataset; the gateway's default of 50 cuts off big containers
ROW_LIMIT = 2000
MAX_RSES_SHOWN = 8

_UNITS = {"B": 1, "bytes": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "PB": 1e15}
# | rse | available_bytes | available_length | state |
_ROW_RE = re.compile(
    r"^\|\s*(?P<rse>[\w\-]+)\s*\|\s*(?P<size>\d+(?:\.\d+)?)\s*(?P<unit>[KMGTP]?B|bytes)\s*\|"
    r"\s*(?P<files>\d+)\s*\|\s*(?P<state>[A-Z_]+)\s*\|$"
)


@dataclass
class RseReplicas:
    datasets: int = 0
    files: int = 0
    bytes: float = 0.0


@dataclass
class ReplicaCheck:
    name: str
    available: dict[str, RseReplicas] = field(default_factory=dict)
    # Replicas per RSE that are not (yet) AVAILABLE, e.g. on scratch disks
    unavailable: dict[str, int] = field(default_factory=dict)
    truncated: bool = False
    error: str | None = None
    not_found: bool = False


def did_for(raw: str) -> tuple[str, str]:
    """(name, scope:name); without a scope, the scope is the name's first field (mc23_13p6TeV, ...)."""
    raw = raw.strip().rstrip("/")
    name = ewkit.parse_dataset(raw).name
    scope = raw.split(":", 1)[0] if ":" in raw else name.split(".", 1)[0]
    return name, f"{scope}:{name}"


def text_of(output: object) -> str:
    """MCP tool output is a string or a list of content blocks."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in output)
    return str(output)


def parse(check: ReplicaCheck, text: str) -> bool:
    """Fold the gateway's replica table into `check`; False when the text has no table rows."""
    rows = 0
    for line in text.splitlines():
        if not (m := _ROW_RE.match(line.strip())):
            continue
        rows += 1
        if m["state"] != "AVAILABLE":
            check.unavailable[m["rse"]] = check.unavailable.get(m["rse"], 0) + 1
            continue
        rse = check.available.setdefault(m["rse"], RseReplicas())
        rse.datasets += 1
        rse.files += int(m["files"])
        rse.bytes += float(m["size"]) * _UNITS[m["unit"]]
    check.truncated = check.truncated or "Pass `offset=" in text
    return rows > 0


def failure(check: ReplicaCheck, text: str) -> None:
    """Record why a check found no table rows; 'No ... replicas found' needs nothing recorded."""
    if not text.startswith("Error"):
        return
    if "not found" in text.lower():
        check.not_found = True
    else:
        details = next((ln for ln in text.splitlines() if ln.startswith("Details:")), text.splitlines()[0])
        check.error = details.removeprefix("Details:").strip()[:300]


def size(n: float) -> str:
    for unit in ("PB", "TB", "GB", "MB", "KB"):
        if n >= _UNITS[unit]:
            return f"{n / _UNITS[unit]:.2f} {unit}"
    return f"{n:.0f} B"


def describe(check: ReplicaCheck, rse: str) -> str:
    if check.error:
        return f"{check.name}: check failed: {check.error}"
    if check.not_found:
        return f"{check.name}: not found in Rucio"
    matching = {k: v for k, v in check.available.items() if rse.lower() in k.lower()}
    unavailable = sum(n for k, n in check.unavailable.items() if rse.lower() in k.lower())
    where = f" at RSEs matching '{rse}'" if rse else ""
    if not matching:
        others = f" (available at {len(check.available)} other RSEs)" if check.available else ""
        return f"{check.name}: no available replicas{where}{others}"
    ranked = sorted(matching.items(), key=lambda kv: kv[1].bytes, reverse=True)
    shown = ", ".join(f"{k} ({v.datasets} datasets, {v.files} files, {size(v.bytes)})" for k, v in ranked[:MAX_RSES_SHOWN])
    more = f", +{len(ranked) - MAX_RSES_SHOWN} more RSEs" if len(ranked) > MAX_RSES_SHOWN else ""
    notes = [f"{unavailable} unavailable replicas"] if unavailable else []
    if check.truncated:
        notes.append(f"partial: Rucio returned more than {ROW_LIMIT} rows")
    tail = f" [{'; '.join(notes)}]" if notes else ""
    return f"{check.name}: available at {len(ranked)} RSEs{where}: {shown}{more}{tail}"


def replica_tools(selection: Selection, mcp_tools: Sequence[BaseTool]) -> list[BaseTool]:
    """check_replicas, when the gateway offers the container replica tool."""
    by_name = {t.name: t for t in mcp_tools}
    container = by_name.get(CONTAINER_TOOL)
    if container is None:
        return []
    dataset = by_name.get(DATASET_TOOL)

    async def check_one(name: str, did: str, limit: asyncio.Semaphore) -> ReplicaCheck:
        check = ReplicaCheck(name=name)
        async with limit:
            try:
                text = text_of(
                    await asyncio.wait_for(container.ainvoke({"did": did, "limit": ROW_LIMIT}), CHECK_TIMEOUT_SECONDS)
                )
                # A plain dataset (not a container) has no child datasets to walk
                if not parse(check, text) and text.startswith("No ") and dataset is not None:
                    text = text_of(
                        await asyncio.wait_for(dataset.ainvoke({"did": did, "limit": ROW_LIMIT}), CHECK_TIMEOUT_SECONDS)
                    )
                    parse(check, text)
                if not check.available and not check.unavailable:
                    failure(check, text)
            except TimeoutError:
                check.error = f"Rucio did not answer within {CHECK_TIMEOUT_SECONDS:.0f}s"
            except Exception as exc:
                check.error = f"{type(exc).__name__}: {str(exc)[:300]}"
        return check

    async def check_replicas(names: list[str] | None = None, rse: str = "") -> str:
        """Check where many datasets have Rucio replicas in one call. Give dataset names (a Rucio scope
        prefix is allowed), or none to check the whole current selection; at most 100 per call.
        Optionally keep only RSEs whose name contains `rse` (e.g. MWT2). Returns one line per dataset:
        the RSEs with available replicas and how many child datasets, files and bytes each holds, or
        'not found in Rucio'. Use this instead of rucio_* replica tools one dataset at a time."""
        wanted = names if names else sorted(selection.selected)
        if not wanted:
            return "The selection is empty and no names were given, so there is nothing to check."
        dids = dict(did_for(raw) for raw in wanted)
        if len(dids) > MAX_DATASETS:
            return (
                f"{len(dids)} datasets is more than {MAX_DATASETS} per call. Ask the person before checking "
                "them all, then pass the names in batches."
            )
        limit = asyncio.Semaphore(MAX_CONCURRENCY)
        checks = await asyncio.gather(*(check_one(name, did, limit) for name, did in dids.items()))
        found = sum(1 for c in checks if c.available)
        missing = sum(1 for c in checks if c.not_found)
        failed = sum(1 for c in checks if c.error)
        header = (
            f"Checked {len(checks)} datasets: {found} with available replicas, {missing} not found in Rucio, "
            f"{failed} checks failed. Per RSE: child datasets of the container with an available replica there."
        )
        return evidence("rucio", header + "\n" + "\n".join(describe(c, rse) for c in checks))

    return [StructuredTool.from_function(coroutine=check_replicas)]
