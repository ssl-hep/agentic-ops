"""Units, their states and the coverage invariant (docs: Bookkeeping, Invariants).

Pure: stdlib and names.py only, deterministic, no I/O, so workflow code can import it.

Every snapshot file is in exactly one live unit, always: a unit that is split or loses files to an
exclusion becomes SUPERSEDED, and its children hold its files between them. Files excluded by a
person sit in EXCLUDED units; files with no disk replica sit in BLOCKED units until a person
excludes them or a refresh finds a replica (planning.py). So the files a dataset still owes are
those of its live units that are not DONE or EXCLUDED.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum

from agentic_ops_worker.names import MEMCLASSES, check_memclass


class UnitState(StrEnum):
    PLANNED = "PLANNED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    DONE = "DONE"
    RETRY_WAIT = "RETRY_WAIT"
    BLOCKED = "BLOCKED"
    NEEDS_DECISION = "NEEDS_DECISION"
    EXCLUDED = "EXCLUDED"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"


S = UnitState

# Where a unit may go from each state. The workflow sees run_unit's outcome without necessarily
# seeing it start, so QUEUED goes everywhere RUNNING does. A unit is split or loses files only while
# nothing runs it (PLANNED, BLOCKED, NEEDS_DECISION).
TRANSITIONS: dict[UnitState, frozenset[UnitState]] = {
    S.PLANNED: frozenset({S.QUEUED, S.EXCLUDED, S.SUPERSEDED, S.CANCELLED}),
    S.QUEUED: frozenset({S.RUNNING, S.VERIFYING, S.RETRY_WAIT, S.BLOCKED, S.NEEDS_DECISION, S.CANCELLED}),
    S.RUNNING: frozenset({S.VERIFYING, S.RETRY_WAIT, S.BLOCKED, S.NEEDS_DECISION, S.CANCELLED}),
    S.VERIFYING: frozenset({S.DONE, S.RETRY_WAIT, S.NEEDS_DECISION, S.CANCELLED}),
    S.RETRY_WAIT: frozenset({S.QUEUED, S.BLOCKED, S.NEEDS_DECISION, S.CANCELLED}),
    S.BLOCKED: frozenset({S.PLANNED, S.NEEDS_DECISION, S.EXCLUDED, S.SUPERSEDED, S.CANCELLED}),
    S.NEEDS_DECISION: frozenset({S.QUEUED, S.RETRY_WAIT, S.BLOCKED, S.EXCLUDED, S.SUPERSEDED, S.CANCELLED}),
    S.DONE: frozenset(),
    S.EXCLUDED: frozenset(),
    S.SUPERSEDED: frozenset(),
    S.CANCELLED: frozenset(),
}

TERMINAL = frozenset(state for state, targets in TRANSITIONS.items() if not targets)
# States whose units hold files that count (every state but SUPERSEDED)
LIVE = frozenset(UnitState) - {S.SUPERSEDED}
# States a unit is finished in, for the dataset to be complete
FINISHED = frozenset({S.DONE, S.EXCLUDED})


class InvalidTransition(ValueError):
    pass


def check_transition(frm: UnitState, to: UnitState) -> UnitState:
    if to not in TRANSITIONS[frm]:
        raise InvalidTransition(f"unit cannot go from {frm} to {to}")
    return to


def next_memclass(memclass: str) -> str | None:
    """The next larger memory class, or None past the largest."""
    i = MEMCLASSES.index(check_memclass(memclass))
    return MEMCLASSES[i + 1] if i + 1 < len(MEMCLASSES) else None


@dataclass(frozen=True)
class UnitSummary:
    """What a dataset workflow keeps per unit: no file list (that stays in Postgres)."""

    unit_id: str
    n_files: int
    events_expected: int
    memclass: str
    state: UnitState
    attempt: int


@dataclass(frozen=True)
class Unit:
    """A unit with its files, as in the units table. Built by planning.py."""

    unit_id: str
    did: str
    # Input files in planning order
    guids: tuple[str, ...]
    size: int
    events_expected: int
    # Estimated EWRun time at first-read rates
    est_seconds: int
    memclass: str
    state: UnitState
    # Attempts started; attempt n writes <unit_id>.a<n>.root
    attempt: int = 0
    # The unit this one was split from
    parent_id: str | None = None

    def with_state(self, to: UnitState) -> "Unit":
        return replace(self, state=check_transition(self.state, to))

    def start_attempt(self, memclass: str | None = None) -> "Unit":
        """QUEUED as a new attempt, optionally on another memory class."""
        return replace(
            self,
            state=check_transition(self.state, S.QUEUED),
            attempt=self.attempt + 1,
            memclass=check_memclass(memclass or self.memclass),
        )

    def summary(self) -> UnitSummary:
        return UnitSummary(
            unit_id=self.unit_id,
            n_files=len(self.guids),
            events_expected=self.events_expected,
            memclass=self.memclass,
            state=self.state,
            attempt=self.attempt,
        )


def coverage_violations(snapshot_guids: Iterable[str], units: Iterable[Unit]) -> list[str]:
    """Breaches of the coverage invariant, empty when it holds.

    - every snapshot file is in exactly one live unit
    - no unit lists a file twice, or a file outside the snapshot
    - a superseded unit's files are exactly those of its children, each in one child
    """
    snapshot = set(snapshot_guids)
    units = list(units)
    problems: list[str] = []
    live: Counter[str] = Counter()
    children: dict[str, list[Unit]] = defaultdict(list)
    for unit in units:
        if unit.parent_id is not None:
            children[unit.parent_id].append(unit)
        dup = sorted(g for g, n in Counter(unit.guids).items() if n > 1)
        if dup:
            problems.append(f"unit {unit.unit_id} lists {', '.join(dup)} more than once")
        unknown = sorted(set(unit.guids) - snapshot)
        if unknown:
            problems.append(f"unit {unit.unit_id} lists {', '.join(unknown)}, not in the snapshot")
        if unit.state in LIVE:
            live.update(set(unit.guids))
    for guid in sorted(snapshot):
        if live[guid] != 1:
            problems.append(f"file {guid} is in {live[guid]} live units")
    for unit in units:
        if unit.state is not S.SUPERSEDED:
            continue
        held = Counter(g for child in children[unit.unit_id] for g in child.guids)
        if set(held) != set(unit.guids) or any(n > 1 for n in held.values()):
            problems.append(f"children of superseded unit {unit.unit_id} do not partition its files")
    return problems


def dataset_complete(units: Iterable[Unit | UnitSummary]) -> bool:
    """Every live unit is DONE or EXCLUDED."""
    return all(unit.state in FINISHED for unit in units if unit.state in LIVE)
