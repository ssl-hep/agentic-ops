"""Unit planning and replanning: splits, exclusions, files staged after planning.

Pure: stdlib, names.py and units.py only, deterministic, no I/O. plan_units runs in the plan_units
activity and the others in replan_unit (both on controller), over the dataset's snapshot.

Units are sized from first-read rates, not cached ones (docs: Reading inputs): a file's estimated
time is its size over the first-read rate of the site it will be read from, plus its events over
EWRun's processing rate. Summing the two, rather than overlapping them, errs towards small units.

Unit IDs are unique within a production, since all its datasets' outputs share one directory:
<number>-<hash8>-u<NNNN>, where <number> is the DID name's second field when it is digits (the
DSID, or the run number for data) and <hash8> the start of the DID's sha256, so that two DIDs of
one DSID (mc23a and mc23d, say) never collide. Split children are <parent>.<k>, k from 1.
"""

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from agentic_ops_worker.names import check_did, check_memclass
from agentic_ops_worker.units import S, Unit, UnitState, check_transition


@dataclass(frozen=True)
class SnapshotFile:
    guid: str
    lfn: str
    size: int
    events: int
    # RSE of the preferred disk replica; None when the file has no disk replica (tape only, lost)
    rse: str | None


@dataclass(frozen=True)
class PlanningParams:
    # Bytes per second of a first read through XCache, for an RSE with no rate of its own. Phase 0
    # measured 25-250 MB/s from the origins tried, a few MB/s at worst.
    default_read_rate: float = 25e6
    read_rate_by_rse: dict[str, float] = field(default_factory=dict)
    # EWRun's processing rate
    events_per_second: float = 200.0
    # Units aim for target_seconds and exceed max_seconds only when one file does
    target_seconds: int = 3 * 3600
    max_seconds: int = 4 * 3600
    max_files: int = 200
    memclass: str = "4g"

    def __post_init__(self) -> None:
        rates = [self.default_read_rate, self.events_per_second, *self.read_rate_by_rse.values()]
        if any(rate <= 0 for rate in rates):
            raise ValueError("rates must be positive")
        if not 0 < self.target_seconds <= self.max_seconds:
            raise ValueError("need 0 < target_seconds <= max_seconds")
        if self.max_files < 1:
            raise ValueError("max_files must be at least 1")
        check_memclass(self.memclass)


def estimate_seconds(file: SnapshotFile, params: PlanningParams) -> float:
    rate = params.read_rate_by_rse.get(file.rse or "", params.default_read_rate)
    return file.size / rate + file.events / params.events_per_second


def unit_prefix(did: str) -> str:
    fields = check_did(did).split(":", 1)[1].split(".")
    digest = hashlib.sha256(did.encode()).hexdigest()[:8]
    if len(fields) > 1 and fields[1].isdigit():
        return f"{fields[1]}-{digest}"
    return digest


def _make_unit(
    unit_id: str,
    did: str,
    files: Sequence[SnapshotFile],
    params: PlanningParams,
    state: UnitState,
    memclass: str,
    parent_id: str | None = None,
) -> Unit:
    return Unit(
        unit_id=unit_id,
        did=did,
        guids=tuple(f.guid for f in files),
        size=sum(f.size for f in files),
        events_expected=sum(f.events for f in files),
        est_seconds=math.ceil(sum(estimate_seconds(f, params) for f in files)),
        memclass=memclass,
        state=state,
        parent_id=parent_id,
    )


def _pack(files: Sequence[SnapshotFile], params: PlanningParams) -> list[list[SnapshotFile]]:
    """Consecutive groups of files. Each group aims at an even share of the time left: the time of
    the files not yet grouped over the number of target-sized units they need."""
    estimates = [estimate_seconds(f, params) for f in files]
    remaining = sum(estimates)
    groups: list[list[SnapshotFile]] = []
    goal = current = 0.0
    for file, est in zip(files, estimates, strict=True):
        # Close the group when this file would take it over a cap, or further past the goal than it
        # is short of it without the file
        if (
            not groups
            or current + est > params.max_seconds
            or len(groups[-1]) >= params.max_files
            or current + est / 2 > goal
        ):
            remaining = max(0.0, remaining)
            goal = remaining / max(1, math.ceil(remaining / params.target_seconds))
            groups.append([])
            current = 0.0
        groups[-1].append(file)
        current += est
        remaining -= est
    return groups


def plan_units(did: str, files: Sequence[SnapshotFile], params: PlanningParams, first_index: int = 0) -> list[Unit]:
    """Units over a dataset's snapshot: PLANNED for files with a disk replica, BLOCKED for files
    without one, packed separately. Files go in LFN order. first_index continues the numbering
    when files are added to a dataset already planned."""
    ordered = sorted(files, key=lambda f: (f.lfn, f.guid))
    if len({f.guid for f in ordered}) != len(ordered):
        raise ValueError("snapshot lists a GUID more than once")
    prefix = unit_prefix(did)
    units: list[Unit] = []
    for state, subset in (
        (S.PLANNED, [f for f in ordered if f.rse is not None]),
        (S.BLOCKED, [f for f in ordered if f.rse is None]),
    ):
        for group in _pack(subset, params):
            unit_id = f"{prefix}-u{first_index + len(units):04d}"
            units.append(_make_unit(unit_id, did, group, params, state, params.memclass))
    return units


def _files_of(unit: Unit, files: Mapping[str, SnapshotFile]) -> list[SnapshotFile]:
    missing = [g for g in unit.guids if g not in files]
    if missing:
        raise ValueError(f"no snapshot entry for {', '.join(missing)}")
    return [files[g] for g in unit.guids]


def split(
    unit: Unit,
    parts: Sequence[Sequence[str]],
    files: Mapping[str, SnapshotFile],
    params: PlanningParams,
    states: Sequence[UnitState] | None = None,
) -> tuple[Unit, list[Unit]]:
    """Supersede a unit with children holding the given parts of its files.

    parts must partition the unit's GUIDs into at least two non-empty parts; each part keeps the
    unit's file order. Children are <unit_id>.<k>, on the unit's memory class, with no attempts,
    PLANNED unless states says otherwise (PLANNED, BLOCKED or EXCLUDED). Returns the superseded
    unit and its children.
    """
    check_transition(unit.state, S.SUPERSEDED)
    if len(parts) < 2 or any(not part for part in parts):
        raise ValueError("a split needs at least two non-empty parts")
    flat = [g for part in parts for g in part]
    if len(flat) != len(set(flat)) or set(flat) != set(unit.guids):
        raise ValueError(f"parts do not partition the files of {unit.unit_id}")
    states = list(states) if states is not None else [S.PLANNED] * len(parts)
    if len(states) != len(parts) or not set(states) <= {S.PLANNED, S.BLOCKED, S.EXCLUDED}:
        raise ValueError("each part needs a state of PLANNED, BLOCKED or EXCLUDED")
    order = {g: i for i, g in enumerate(unit.guids)}
    in_order = _files_of(unit, files)
    children = [
        _make_unit(
            f"{unit.unit_id}.{k}",
            unit.did,
            [in_order[order[g]] for g in sorted(part, key=order.__getitem__)],
            params,
            state,
            unit.memclass,
            parent_id=unit.unit_id,
        )
        for k, (part, state) in enumerate(zip(parts, states, strict=True), start=1)
    ]
    return unit.with_state(S.SUPERSEDED), children


def split_even(
    unit: Unit, n: int, files: Mapping[str, SnapshotFile], params: PlanningParams
) -> tuple[Unit, list[Unit]]:
    """Split a unit into n consecutive parts of about equal estimated time."""
    if not 2 <= n <= len(unit.guids):
        raise ValueError(f"cannot split {len(unit.guids)} files into {n} parts")
    weights = [estimate_seconds(f, params) for f in _files_of(unit, files)]
    if sum(weights) == 0:
        weights = [1.0] * len(weights)
    total = sum(weights)
    prefix = [0.0]
    for w in weights:
        prefix.append(prefix[-1] + w)
    cuts = [0]
    for k in range(1, n):
        # First cut at or past k/n of the total, leaving at least one file per part either side
        i = next((i for i in range(1, len(weights)) if prefix[i] >= total * k / n), len(weights))
        cuts.append(min(max(i, cuts[-1] + 1), len(weights) - (n - k)))
    cuts.append(len(weights))
    parts = [unit.guids[a:b] for a, b in zip(cuts, cuts[1:], strict=False)]
    return split(unit, parts, files, params)


def exclude_files(
    unit: Unit, guids: Sequence[str], files: Mapping[str, SnapshotFile], params: PlanningParams
) -> tuple[Unit, list[Unit]]:
    """Exclude some of a unit's files (approved by a person).

    All of them: the unit itself becomes EXCLUDED. Some: it is superseded by a child with the kept
    files (PLANNED, or BLOCKED if the unit was) and an EXCLUDED child with the others.
    """
    excluded = set(guids)
    if not excluded or not excluded <= set(unit.guids):
        raise ValueError(f"files to exclude must be some of the files of {unit.unit_id}")
    if excluded == set(unit.guids):
        return unit.with_state(S.EXCLUDED), []
    kept = [g for g in unit.guids if g not in excluded]
    gone = [g for g in unit.guids if g in excluded]
    kept_state = S.BLOCKED if unit.state is S.BLOCKED else S.PLANNED
    return split(unit, [kept, gone], files, params, [kept_state, S.EXCLUDED])


def unblock(unit: Unit, files: Mapping[str, SnapshotFile], params: PlanningParams) -> tuple[Unit, list[Unit]]:
    """A BLOCKED unit after a replica refresh: files now on disk can be planned.

    All of them: the unit becomes PLANNED with its estimate redone. Some: it is superseded by a
    PLANNED child with those and a BLOCKED child with the rest. None: the unit is unchanged.
    """
    if unit.state is not S.BLOCKED:
        raise ValueError(f"unit {unit.unit_id} is {unit.state}, not BLOCKED")
    current = _files_of(unit, files)
    ready = [f.guid for f in current if f.rse is not None]
    if not ready:
        return unit, []
    if len(ready) == len(current):
        planned = _make_unit(unit.unit_id, unit.did, current, params, S.PLANNED, unit.memclass, unit.parent_id)
        return replace(planned, attempt=unit.attempt), []
    waiting = [f.guid for f in current if f.rse is None]
    return split(unit, [ready, waiting], files, params, [S.PLANNED, S.BLOCKED])
