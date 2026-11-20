"""The SQL invariants agree with units.coverage_violations.

A hypothesis state machine plans a dataset, splits, excludes, stages and runs its units, and now
and then breaks coverage the way a bug would (a file in a second unit, a unit superseded with no
children). After every step it records the units in Postgres and checks that the
invariant_violations view names the same files and superseded units as the pure check, and
nothing else.

Hypothesis state machines are synchronous, so the database calls run on an event loop in a thread
of their own; each example is a new production in one test database.
"""

import asyncio
import re
import threading
from collections.abc import Coroutine
from dataclasses import replace
from datetime import timedelta
from typing import Any

import psycopg
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)
from test_reporting import DID, MWT2, T0, new_production

from agentic_ops_worker import records as r
from agentic_ops_worker import reporting as rep
from agentic_ops_worker.planning import PlanningParams, SnapshotFile, exclude_files, plan_units, split_even, unblock
from agentic_ops_worker.signatures import Action, Proposal
from agentic_ops_worker.units import LIVE, S, Unit, UnitState, coverage_violations

FILE_RE = re.compile(r"file (\S+) is in \d+ live units")
PARENT_RE = re.compile(r"children of superseded unit (\S+) do not partition its files")


class LoopThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=60)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()
        self.loop.close()


class Projection(RuleBasedStateMachine):
    loop: LoopThread
    conn: psycopg.AsyncConnection

    @initialize(n=st.integers(1, 12), tape=st.sets(st.integers(0, 11)), seed=st.randoms(use_true_random=False))
    def plan(self, n: int, tape: set[int], seed) -> None:
        self.random = seed
        self.seq = 1
        self.decisions = 0
        self.corrupted = False
        records = [
            r.SnapshotFileRecord(
                f"g{i:03d}",
                f"DAOD.{i:06d}.root",
                seed.randrange(10**8, 10**10),
                f"ad:{i:08x}",
                seed.randrange(0, 50_000),
                [] if i in tape else [r.Replica("MWT2_DATADISK", f"{MWT2}/{i}")],
            )
            for i in range(n)
        ]
        self.files: dict[str, SnapshotFile] = {f.guid: f.planning_file() for f in records}
        self.params = PlanningParams(target_seconds=1800, max_seconds=3600, max_files=4)
        self.units = {u.unit_id: u for u in plan_units(DID, list(self.files.values()), self.params)}
        self.production_id = self.loop.run(new_production(self.conn))
        self.loop.run(rep.write_snapshot(self.conn, r.SnapshotPage(self.production_id, DID, T0, records)))
        self._record(list(self.units.values()))

    def _record(self, units: list[Unit], decision_id: str | None = None) -> None:
        self.seq += 1
        records = [r.UnitRecord.of(u, decision_id=decision_id if u.state is S.EXCLUDED else None) for u in units]
        self.loop.run(rep.write_units(self.conn, r.UnitsWrite(self.production_id, self.seq, records)))
        self.units.update({u.unit_id: u for u in units})

    def _pick(self, states: set[UnitState], min_files: int = 1) -> Unit | None:
        choices = [u for u in self.units.values() if u.state in states and len(u.guids) >= min_files]
        return self.random.choice(sorted(choices, key=lambda u: u.unit_id)) if choices else None

    @rule(n=st.integers(2, 4))
    def split_evenly(self, n: int) -> None:
        unit = self._pick({S.PLANNED, S.NEEDS_DECISION}, min_files=n)
        if unit is not None:
            parent, children = split_even(unit, n, self.files, self.params)
            self._record([parent, *children])

    @rule(data=st.data())
    def exclude(self, data: st.DataObject) -> None:
        unit = self._pick({S.PLANNED, S.BLOCKED, S.NEEDS_DECISION})
        if unit is None:
            return
        guids = data.draw(st.lists(st.sampled_from(unit.guids), min_size=1, unique=True))
        self.decisions += 1
        decision_id = f"dataset/{self.production_id}/{DID}/d{self.decisions}"
        proposal = Proposal(Action.EXCLUDE_FILES, "agent", "bad files", guids=guids)
        decision = r.DecisionRecord(decision_id, self.production_id, proposal, "approved", "user:bengal1", T0)
        self.loop.run(rep.write_decision(self.conn, decision))
        parent, children = exclude_files(unit, guids, self.files, self.params)
        self._record([parent, *children], decision_id)

    @rule(data=st.data())
    def stage(self, data: st.DataObject) -> None:
        unit = self._pick({S.BLOCKED})
        if unit is None:
            return
        staged = data.draw(st.lists(st.sampled_from(unit.guids), unique=True))
        for guid in staged:
            self.files[guid] = replace(self.files[guid], rse="BNL-OSG2_DATADISK")
        at = T0 + timedelta(minutes=self.seq)
        refresh = [r.FileReplicas(g, [r.Replica("BNL-OSG2_DATADISK", f"root://bnl//{g}")]) for g in staged]
        self.loop.run(rep.replace_replicas(self.conn, r.ReplicaRefresh(self.production_id, DID, at, refresh)))
        parent, children = unblock(unit, self.files, self.params)
        self._record([parent, *children])

    @rule()
    def run_to_done(self) -> None:
        unit = self._pick({S.PLANNED})
        if unit is None:
            return
        done = unit.start_attempt().with_state(S.VERIFYING).with_state(S.DONE)
        attempt = r.AttemptRecord(
            self.production_id, unit.unit_id, done.attempt, outcome="succeeded", verification="accepted"
        )
        self._record([done])
        self.loop.run(rep.write_attempt(self.conn, attempt))

    @precondition(lambda self: not self.corrupted)
    @rule()
    def put_a_file_in_a_second_unit(self) -> None:
        unit = self._pick(set(LIVE))
        if unit is None:
            return
        guid = unit.guids[0]
        extra = replace(unit, unit_id=f"{unit.unit_id}.x", guids=(guid,), events_expected=self.files[guid].events)
        self.corrupted = True
        self._record([replace(extra, state=S.PLANNED, parent_id=None, attempt=0)])

    @precondition(lambda self: not self.corrupted)
    @rule()
    def supersede_without_children(self) -> None:
        unit = self._pick({S.PLANNED, S.BLOCKED})
        if unit is None:
            return
        self.corrupted = True
        self._record([replace(unit, state=S.SUPERSEDED)])

    @invariant()
    def sql_agrees_with_coverage_violations(self) -> None:
        report = self.loop.run(
            rep.invariant_violations(self.conn, r.InvariantsRequest(self.production_id, limit=10_000))
        )
        pure = coverage_violations(self.files, self.units.values())
        assert {v.guid for v in report.violations if v.kind == "file_not_in_one_live_unit"} == {
            m[1] for p in pure if (m := FILE_RE.fullmatch(p))
        }
        assert {v.unit_id for v in report.violations if v.kind == "children_do_not_partition"} == {
            m[1] for p in pure if (m := PARENT_RE.fullmatch(p))
        }
        assert {v.kind for v in report.violations} <= {"file_not_in_one_live_unit", "children_do_not_partition"}
        # Planning keeps coverage, and neither corruption is ever undone
        assert bool(report.violations) == bool(pure) == self.corrupted


def test_sql_invariants_agree_with_coverage_violations(db: str) -> None:
    loop = LoopThread()
    conn = loop.run(psycopg.AsyncConnection.connect(db, autocommit=True))
    try:

        class Machine(Projection):
            pass

        Machine.loop, Machine.conn = loop, conn
        run_state_machine_as_test(Machine, settings=settings(max_examples=40, stateful_step_count=15, deadline=None))
    finally:
        loop.run(conn.close())
        loop.close()
