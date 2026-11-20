"""The unit state machine, splits and exclusions, and the coverage invariant under any sequence
of them (a hypothesis state machine)."""

from dataclasses import replace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

from agentic_ops_worker.planning import (
    PlanningParams,
    SnapshotFile,
    exclude_files,
    plan_units,
    split,
    split_even,
    unblock,
)
from agentic_ops_worker.units import (
    LIVE,
    TERMINAL,
    TRANSITIONS,
    InvalidTransition,
    S,
    Unit,
    UnitState,
    check_transition,
    coverage_violations,
    dataset_complete,
    next_memclass,
)

DID = "mc23_13p6TeV:mc23_13p6TeV.601189.PhPy8EG_AZNLO_Zee.deriv.DAOD_PHYS.e8514_s4162_r15540_p6266"
PARAMS = PlanningParams()


def snapshot(n: int, tape: frozenset[int] = frozenset()) -> dict[str, SnapshotFile]:
    return {
        f"g{i:03d}": SnapshotFile(
            f"g{i:03d}", f"DAOD.{i:06d}.root", 5_000_000_000, 10_000, None if i in tape else "MWT2_DATADISK"
        )
        for i in range(n)
    }


def one_unit(files: dict[str, SnapshotFile], state: UnitState = S.PLANNED) -> Unit:
    params = PlanningParams(target_seconds=10**9, max_seconds=10**9, max_files=10**6)
    (unit,) = plan_units(DID, list(files.values()), params)
    return replace(unit, state=state)


# --- state machine -----------------------------------------------------------------------------


def test_every_state_has_transitions_listed() -> None:
    assert set(TRANSITIONS) == set(UnitState)
    assert TERMINAL == {S.DONE, S.EXCLUDED, S.SUPERSEDED, S.CANCELLED}
    assert LIVE == set(UnitState) - {S.SUPERSEDED}


@pytest.mark.parametrize("frm", list(UnitState))
@pytest.mark.parametrize("to", list(UnitState))
def test_transitions(frm: UnitState, to: UnitState) -> None:
    if to in TRANSITIONS[frm]:
        assert check_transition(frm, to) is to
    else:
        with pytest.raises(InvalidTransition):
            check_transition(frm, to)


def test_every_non_terminal_state_can_be_cancelled_and_reach_terminal() -> None:
    for state in set(UnitState) - TERMINAL:
        assert S.CANCELLED in TRANSITIONS[state]


def test_split_only_while_nothing_runs_it() -> None:
    assert {s for s, targets in TRANSITIONS.items() if S.SUPERSEDED in targets} == {
        S.PLANNED,
        S.BLOCKED,
        S.NEEDS_DECISION,
    }


def test_attempts() -> None:
    unit = one_unit(snapshot(2))
    first = unit.start_attempt()
    assert (first.state, first.attempt, first.memclass) == (S.QUEUED, 1, "4g")
    retried = first.with_state(S.RUNNING).with_state(S.NEEDS_DECISION).start_attempt("8g")
    assert (retried.state, retried.attempt, retried.memclass) == (S.QUEUED, 2, "8g")
    with pytest.raises(InvalidTransition):
        retried.start_attempt()
    with pytest.raises(ValueError):
        unit.start_attempt("2g")
    summary = retried.summary()
    assert (summary.unit_id, summary.n_files, summary.events_expected) == (unit.unit_id, 2, 20_000)


def test_next_memclass() -> None:
    assert [next_memclass(m) for m in ("4g", "8g", "16g")] == ["8g", "16g", None]
    with pytest.raises(ValueError):
        next_memclass("2g")


# --- splits and exclusions ---------------------------------------------------------------------


def test_split() -> None:
    files = snapshot(5)
    unit = one_unit(files)
    parent, children = split(unit, [["g000", "g003"], ["g004", "g001", "g002"]], files, PARAMS)
    assert parent.state is S.SUPERSEDED
    assert [c.unit_id for c in children] == [f"{unit.unit_id}.1", f"{unit.unit_id}.2"]
    assert children[1].guids == ("g001", "g002", "g004")  # the unit's order, not the part's
    assert all(c.parent_id == unit.unit_id and c.attempt == 0 and c.state is S.PLANNED for c in children)
    assert sum(c.events_expected for c in children) == unit.events_expected
    assert coverage_violations(files, [parent, *children]) == []
    grand, grandchildren = split(children[1], [["g001"], ["g002", "g004"]], files, PARAMS)
    assert grandchildren[0].unit_id == f"{unit.unit_id}.2.1"


@pytest.mark.parametrize(
    "parts",
    [[["g000", "g001", "g002"]], [["g000"], ["g001"]], [["g000", "g001"], ["g001", "g002"]], [["g000"], []]],
)
def test_split_needs_a_partition(parts: list[list[str]]) -> None:
    files = snapshot(3)
    with pytest.raises(ValueError):
        split(one_unit(files), parts, files, PARAMS)


def test_no_split_while_running() -> None:
    files = snapshot(2)
    with pytest.raises(InvalidTransition):
        split(one_unit(files).start_attempt(), [["g000"], ["g001"]], files, PARAMS)


def test_split_even() -> None:
    files = snapshot(7)
    _, children = split_even(one_unit(files), 3, files, PARAMS)
    assert [len(c.guids) for c in children] == [3, 2, 2]
    _, children = split_even(one_unit(files), 7, files, PARAMS)
    assert [len(c.guids) for c in children] == [1] * 7
    with pytest.raises(ValueError):
        split_even(one_unit(files), 8, files, PARAMS)


def test_exclude_some_files() -> None:
    files = snapshot(4)
    unit = one_unit(files, S.NEEDS_DECISION)
    parent, (kept, gone) = exclude_files(unit, ["g002"], files, PARAMS)
    assert parent.state is S.SUPERSEDED
    assert (kept.guids, kept.state) == (("g000", "g001", "g003"), S.PLANNED)
    assert (gone.guids, gone.state) == (("g002",), S.EXCLUDED)
    assert coverage_violations(files, [parent, kept, gone]) == []
    assert not dataset_complete([parent, kept, gone])


def test_exclude_all_files() -> None:
    files = snapshot(2)
    unit, children = exclude_files(one_unit(files, S.BLOCKED), ["g001", "g000"], files, PARAMS)
    assert (unit.state, children) == (S.EXCLUDED, [])
    assert dataset_complete([unit])
    with pytest.raises(ValueError):
        exclude_files(one_unit(files), ["g009"], files, PARAMS)


def test_unblock() -> None:
    files = snapshot(3, tape=frozenset({0, 1, 2}))
    (unit,) = plan_units(DID, list(files.values()), PARAMS)
    assert unit.state is S.BLOCKED
    assert unblock(unit, files, PARAMS) == (unit, [])
    files["g001"] = SnapshotFile("g001", "DAOD.000001.root", 5_000_000_000, 10_000, "MWT2_DATADISK")
    parent, (ready, waiting) = unblock(unit, files, PARAMS)
    assert (ready.guids, ready.state, waiting.guids, waiting.state) == (
        ("g001",),
        S.PLANNED,
        ("g000", "g002"),
        S.BLOCKED,
    )
    for g in ("g000", "g002"):
        files[g] = SnapshotFile(g, files[g].lfn, files[g].size, files[g].events, "BNL_DATADISK")
    planned, children = unblock(waiting, files, PARAMS)
    assert (planned.state, planned.unit_id, children) == (S.PLANNED, waiting.unit_id, [])


def test_coverage_violations() -> None:
    files = snapshot(3)
    unit = one_unit(files)
    assert coverage_violations(files, [unit]) == []
    assert coverage_violations({**files, "g999": files["g000"]}, [unit]) == ["file g999 is in 0 live units"]
    assert coverage_violations(files, [unit, unit]) == [f"file {g} is in 2 live units" for g in sorted(files)]
    parent, children = split(unit, [["g000"], ["g001", "g002"]], files, PARAMS)
    problems = coverage_violations(files, [parent, children[0]])
    assert problems[-1] == f"children of superseded unit {unit.unit_id} do not partition its files"
    assert "not in the snapshot" in coverage_violations(snapshot(2), [unit])[0]


# --- property: coverage holds under any sequence of replanning ---------------------------------


class Replanning(RuleBasedStateMachine):
    """A dataset's units under random splits, exclusions, stagings and attempts."""

    @initialize(n=st.integers(1, 30), tape=st.sets(st.integers(0, 29)), seed=st.randoms(use_true_random=False))
    def plan(self, n: int, tape: set[int], seed) -> None:
        self.random = seed
        self.files = {
            f"g{i:03d}": SnapshotFile(
                f"g{i:03d}",
                f"DAOD.{i:06d}.root",
                seed.randrange(10**8, 10**10),
                seed.randrange(0, 50_000),
                None if i in tape else seed.choice(["MWT2_DATADISK", "TRIUMF-LCG2_DATADISK"]),
            )
            for i in range(n)
        }
        params = PlanningParams(target_seconds=1800, max_seconds=3600, max_files=7)
        self.params = params
        self.units = {u.unit_id: u for u in plan_units(DID, list(self.files.values()), params)}

    def _pick(self, states: set[UnitState], min_files: int = 1) -> Unit | None:
        choices = [u for u in self.units.values() if u.state in states and len(u.guids) >= min_files]
        return self.random.choice(sorted(choices, key=lambda u: u.unit_id)) if choices else None

    def _apply(self, updated: Unit, children: list[Unit]) -> None:
        for unit in (updated, *children):
            assert unit.unit_id not in self.units or unit.unit_id == updated.unit_id
            self.units[unit.unit_id] = unit

    @rule(data=st.data())
    def split_unit(self, data: st.DataObject) -> None:
        unit = self._pick({S.PLANNED, S.BLOCKED, S.NEEDS_DECISION}, min_files=2)
        if unit is None:
            return
        labels = data.draw(st.lists(st.integers(0, 3), min_size=len(unit.guids), max_size=len(unit.guids)))
        parts = [[g for g, label in zip(unit.guids, labels, strict=True) if label == k] for k in range(4)]
        parts = [p for p in parts if p]
        if len(parts) < 2:
            parts = [list(unit.guids[:1]), list(unit.guids[1:])]
        self._apply(*split(unit, parts, self.files, self.params))

    @rule(n=st.integers(2, 5))
    def split_evenly(self, n: int) -> None:
        unit = self._pick({S.PLANNED, S.NEEDS_DECISION}, min_files=n)
        if unit is not None:
            self._apply(*split_even(unit, n, self.files, self.params))

    @rule(data=st.data())
    def exclude(self, data: st.DataObject) -> None:
        unit = self._pick({S.PLANNED, S.BLOCKED, S.NEEDS_DECISION})
        if unit is None:
            return
        guids = data.draw(st.lists(st.sampled_from(unit.guids), min_size=1, unique=True))
        self._apply(*exclude_files(unit, guids, self.files, self.params))

    @rule(data=st.data())
    def stage(self, data: st.DataObject) -> None:
        unit = self._pick({S.BLOCKED})
        if unit is None:
            return
        for guid in data.draw(st.lists(st.sampled_from(unit.guids), unique=True)):
            f = self.files[guid]
            self.files[guid] = SnapshotFile(f.guid, f.lfn, f.size, f.events, "BNL-OSG2_DATADISK")
        self._apply(*unblock(unit, self.files, self.params))

    @rule()
    def run_to_done(self) -> None:
        unit = self._pick({S.PLANNED})
        if unit is not None:
            self._apply(unit.start_attempt().with_state(S.VERIFYING).with_state(S.DONE), [])

    @rule()
    def fail_to_decision(self) -> None:
        unit = self._pick({S.PLANNED})
        if unit is not None:
            self._apply(unit.start_attempt().with_state(S.RUNNING).with_state(S.NEEDS_DECISION), [])

    @invariant()
    def every_file_in_exactly_one_live_unit(self) -> None:
        assert coverage_violations(self.files, self.units.values()) == []

    @invariant()
    def events_are_conserved(self) -> None:
        live = sum(u.events_expected for u in self.units.values() if u.state in LIVE)
        assert live == sum(f.events for f in self.files.values())

    @precondition(lambda self: all(u.state in TERMINAL for u in self.units.values()))
    @invariant()
    def complete_when_all_finished(self) -> None:
        assert dataset_complete(self.units.values())


Replanning.TestCase.settings = settings(max_examples=200, stateful_step_count=30, deadline=None)
TestReplanning = Replanning.TestCase


@given(st.sampled_from(sorted(set(UnitState) - TERMINAL)))
def test_summary_keeps_state(state: UnitState) -> None:
    assert one_unit(snapshot(1), state).summary().state is state
