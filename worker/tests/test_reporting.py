"""The reporting writes are idempotent and refuse stale data; the invariant and what-remains views
report what they should; the activities wrap both (against Postgres in Docker, conftest.py)."""

import dataclasses
import json
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from agentic_ops_worker import records as r
from agentic_ops_worker import reporting as rep
from agentic_ops_worker.contracts import Cut, FailedRead, Manifest, ReplicaRead, UnreadableFile
from agentic_ops_worker.planning import PlanningParams, SnapshotFile, exclude_files, plan_units
from agentic_ops_worker.signatures import Action, Proposal
from agentic_ops_worker.units import S, Unit

DID = "mc23_13p6TeV:mc23_13p6TeV.601189.PhPy8EG_AZNLO_Zee.deriv.DAOD_PHYS.e8514_s4162_r15540_p6266"
T0 = datetime(2026, 11, 20, 12, tzinfo=UTC)
# 1 GB and 10k events per file is 90 s of EWRun: units of 3 files, by max_files
PARAMS = PlanningParams(target_seconds=1800, max_seconds=3600, max_files=3)
MWT2 = "root://fax.mwt2.org:1094//pnfs/uchicago.edu/atlasdatadisk/rucio/mc23_13p6TeV"
BNL = "root://dcdoor16.usatlas.bnl.gov:1094//pnfs/usatlas.bnl.gov/atlasdatadisk/rucio/mc23_13p6TeV"

Conn = psycopg.AsyncConnection


def snapshot_files(n: int, tape: tuple[int, ...] = ()) -> list[r.SnapshotFileRecord]:
    return [
        r.SnapshotFileRecord(
            guid=f"g{i:03d}",
            lfn=f"DAOD_PHYS.40123456._{i:06d}.pool.root.1",
            size=1_000_000_000,
            checksum=f"ad:{i:08x}",
            events=10_000,
            replicas=[]
            if i in tape
            else [r.Replica("MWT2_DATADISK", f"{MWT2}/{i}"), r.Replica("BNL-OSG2_DATADISK", f"{BNL}/{i}")],
        )
        for i in range(n)
    ]


async def new_production(conn: Conn) -> str:
    production_id = f"p{uuid.uuid4().hex[:12]}"
    await rep.create_selection(
        conn,
        rep.Selection(f"sel-{production_id}", "ZeeAnalysis", "sha256:00", "bengal1", [DID], "bengal1", T0),
    )
    await rep.create_production(
        conn,
        rep.Production(
            production_id=production_id,
            analysis="ZeeAnalysis",
            config_name="zee.yaml",
            clone_path="/home/bengal1/EWKit",
            commit_sha="0123456789abcdef0123456789abcdef01234567",
            release="25.2.75",
            tarball_path=f"/data/bengal1/agentic-ops/builds/{production_id}/build.tar.gz",
            tarball_digest="sha256:11",
            tag="v1",
            selection_id=f"sel-{production_id}",
            owner="bengal1",
        ),
    )
    return production_id


@dataclass
class Planned:
    production_id: str
    files: dict[str, SnapshotFile]
    units: list[Unit]


async def planned(conn: Conn, n: int = 8, tape: tuple[int, ...] = (6, 7)) -> Planned:
    """A recorded dataset of n files: units of 3 disk files PLANNED, the tape files BLOCKED."""
    production_id = await new_production(conn)
    files = snapshot_files(n, tape)
    await rep.write_snapshot(conn, r.SnapshotPage(production_id, DID, T0, files))
    units = plan_units(DID, [f.planning_file() for f in files], PARAMS)
    await rep.write_units(conn, r.UnitsWrite(production_id, 1, [r.UnitRecord.of(u) for u in units]))
    return Planned(production_id, {f.guid: f.planning_file() for f in files}, units)


async def rows(conn: Conn, query: str, *params: Any) -> list[tuple]:
    cur = await conn.execute(query, params)
    return await cur.fetchall()


async def kinds(conn: Conn, production_id: str) -> set[str]:
    report = await rep.invariant_violations(conn, r.InvariantsRequest(production_id))
    return {v.kind for v in report.violations}


def manifest(unit_id: str, attempt: int, started: datetime, finished: datetime, reads: list[ReplicaRead]) -> Manifest:
    return Manifest(
        unit_id=unit_id,
        attempt=attempt,
        status="succeeded",
        exit_code=0,
        events_processed=30_000,
        sum_of_weights=1.5e7,
        cutflow=[Cut("all", 30_000, 1.5e7), Cut("two electrons", 9_000, 4.4e6)],
        output_path=f"/data/out/{unit_id}.a{attempt}.root",
        size=123_456,
        checksum="sha256:ab",
        replicas=reads,
        host="c014.af.uchicago.edu",
        condor_job="4417203.12",
        started=started,
        finished=finished,
        log_path=f"/data/logs/{unit_id}.a{attempt}.log",
    )


# --- snapshot and replicas ---------------------------------------------------------------------


async def test_snapshot_is_written_once(conn: Conn) -> None:
    production_id = await new_production(conn)
    page = r.SnapshotPage(production_id, DID, T0, snapshot_files(4, tape=(3,)))
    await rep.write_snapshot(conn, page)
    await rep.write_snapshot(conn, page)
    await rep.write_snapshot(conn, replace(page, snapshot_at=T0 + timedelta(hours=1)))
    assert await rows(
        conn, "SELECT count(*) FROM production.dataset_snapshots WHERE production_id = %s", production_id
    ) == [(4,)]
    assert await rows(
        conn, "SELECT count(*) FROM production.file_replicas WHERE production_id = %s", production_id
    ) == [(6,)]
    assert await rows(
        conn, "SELECT state, snapshot_at, seq FROM production.datasets WHERE production_id = %s", production_id
    ) == [("PLANNING", T0, 0)]


async def test_snapshot_needs_a_production(conn: Conn) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await rep.write_snapshot(conn, r.SnapshotPage("nosuchproduction", DID, T0, snapshot_files(1)))


async def replicas_of(conn: Conn, production_id: str, guid: str) -> list[tuple]:
    return await rows(
        conn,
        "SELECT rse, rank, resolved_at FROM production.file_replicas WHERE production_id = %s AND guid = %s"
        " ORDER BY rank",
        production_id,
        guid,
    )


async def test_refresh_replaces_replicas_unless_stale(conn: Conn) -> None:
    production_id = await new_production(conn)
    files = snapshot_files(2, tape=(1,))
    await rep.write_snapshot(conn, r.SnapshotPage(production_id, DID, T0, files))
    t1, t2 = T0 + timedelta(hours=1), T0 + timedelta(hours=2)
    staged = r.FileReplicas("g001", [r.Replica("BNL-OSG2_DATADISK", f"{BNL}/1")])
    moved = r.FileReplicas("g000", [r.Replica("AGLT2_DATADISK", "root://aglt2/0")])

    await rep.replace_replicas(conn, r.ReplicaRefresh(production_id, DID, t2, [staged, moved]))
    assert await replicas_of(conn, production_id, "g000") == [("AGLT2_DATADISK", 0, t2)]
    assert await replicas_of(conn, production_id, "g001") == [("BNL-OSG2_DATADISK", 0, t2)]
    # An older refresh committing late, and a retry of the same one, change nothing
    gone = r.FileReplicas("g000", [])
    await rep.replace_replicas(conn, r.ReplicaRefresh(production_id, DID, t1, [gone]))
    await rep.replace_replicas(conn, r.ReplicaRefresh(production_id, DID, t2, [gone]))
    assert await replicas_of(conn, production_id, "g000") == [("AGLT2_DATADISK", 0, t2)]
    # Nor does the snapshot page, repeated
    await rep.write_snapshot(conn, r.SnapshotPage(production_id, DID, T0, files))
    assert await replicas_of(conn, production_id, "g000") == [("AGLT2_DATADISK", 0, t2)]

    with pytest.raises(rep.ReportingConflict, match="g999"):
        await rep.replace_replicas(conn, r.ReplicaRefresh(production_id, DID, t2, [r.FileReplicas("g999", [])]))


# --- units -------------------------------------------------------------------------------------


async def test_units_are_written_once_with_their_files(conn: Conn) -> None:
    p = await planned(conn)
    assert [u.state for u in p.units] == [S.PLANNED, S.PLANNED, S.BLOCKED]
    write = r.UnitsWrite(p.production_id, 1, [r.UnitRecord.of(u) for u in p.units])
    await rep.write_units(conn, write)
    await rep.write_units(conn, write)
    got = await rows(
        conn,
        "SELECT unit_id, array_agg(guid ORDER BY ordinal) FROM production.unit_files WHERE production_id = %s"
        " GROUP BY unit_id ORDER BY unit_id",
        p.production_id,
    )
    assert got == [(u.unit_id, list(u.guids)) for u in p.units]
    assert await kinds(conn, p.production_id) == set()

    other = r.UnitRecord.of(replace(p.units[0], guids=p.units[0].guids[:2]))
    with pytest.raises(rep.ReportingConflict, match="other files"):
        await rep.write_units(conn, r.UnitsWrite(p.production_id, 2, [other]))


async def test_unit_files_must_be_in_the_snapshot(conn: Conn) -> None:
    p = await planned(conn)
    stray = r.UnitRecord.of(replace(p.units[0], unit_id="x-u9999", guids=("g000", "nosuchguid")))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await rep.write_units(conn, r.UnitsWrite(p.production_id, 2, [stray]))


async def test_split_parent_and_children_in_one_write(conn: Conn) -> None:
    p = await planned(conn)
    parent, children = exclude_files(p.units[1], ["g005"], p.files, PARAMS)
    records = [r.UnitRecord.of(u) for u in children] + [r.UnitRecord.of(parent)]  # children first: FK deferred
    await rep.write_units(conn, r.UnitsWrite(p.production_id, 2, records))
    got = await rows(
        conn,
        "SELECT unit_id, state, parent_id FROM production.units WHERE production_id = %s AND unit_id LIKE %s"
        " ORDER BY unit_id",
        p.production_id,
        f"{parent.unit_id}%",
    )
    assert got == [
        (parent.unit_id, "SUPERSEDED", None),
        (f"{parent.unit_id}.1", "PLANNED", parent.unit_id),
        (f"{parent.unit_id}.2", "EXCLUDED", parent.unit_id),
    ]


async def unit_row(conn: Conn, production_id: str, unit_id: str) -> tuple:
    (row,) = await rows(
        conn,
        "SELECT state, attempt, memclass, seq FROM production.units WHERE production_id = %s AND unit_id = %s",
        production_id,
        unit_id,
    )
    return row


async def test_unit_state_applies_only_a_larger_seq(conn: Conn) -> None:
    p = await planned(conn)
    unit = p.units[0].unit_id

    def state(s: str, attempt: int, seq: int, memclass: str = "4g") -> r.UnitStateRecord:
        return r.UnitStateRecord(p.production_id, unit, s, attempt, memclass, failures=0, seq=seq)

    await rep.write_unit_state(conn, state("QUEUED", 1, 3))
    assert await unit_row(conn, p.production_id, unit) == ("QUEUED", 1, "4g", 3)
    # A try that timed out but committed late, and a retry with the same seq
    await rep.write_unit_state(conn, state("PLANNED", 0, 2))
    await rep.write_unit_state(conn, state("RUNNING", 1, 3))
    assert await unit_row(conn, p.production_id, unit) == ("QUEUED", 1, "4g", 3)
    await rep.write_unit_state(conn, state("QUEUED", 2, 4, "8g"))
    assert await unit_row(conn, p.production_id, unit) == ("QUEUED", 2, "8g", 4)
    # A plan written again later (seq 1) does not undo it either
    await rep.write_units(conn, r.UnitsWrite(p.production_id, 1, [r.UnitRecord.of(p.units[0])]))
    assert await unit_row(conn, p.production_id, unit) == ("QUEUED", 2, "8g", 4)

    with pytest.raises(rep.ReportingConflict, match="never recorded"):
        await rep.write_unit_state(conn, replace(state("QUEUED", 1, 9), unit_id="nosuchunit"))


# --- attempts ----------------------------------------------------------------------------------


async def test_attempt_fields_are_written_once(conn: Conn) -> None:
    p = await planned(conn)
    unit = p.units[0].unit_id
    key = (p.production_id, unit, 1)
    queued = r.AttemptRecord(*key, memclass="4g", queued_at=T0)
    reads = [ReplicaRead("g000", f"{BNL}/0", failed=[FailedRead(f"{MWT2}/0", "[ERROR] Operation expired")])]
    started, finished = T0 + timedelta(minutes=5), T0 + timedelta(hours=2)
    ran = r.attempt_from_manifest(p.production_id, manifest(unit, 1, started, finished, reads), "/data/m/x.json")

    await rep.write_attempt(conn, queued)
    await rep.write_attempt(conn, ran)
    await rep.write_attempt(conn, queued)
    await rep.write_attempt(conn, replace(ran, outcome="failed", host="elsewhere", memclass="8g"))
    got = await rows(
        conn,
        "SELECT memclass, queued_at, started_at, finished_at, outcome, host, manifest->>'condor_job',"
        " manifest->'cutflow'->1->>'name', verification FROM production.attempts"
        " WHERE (production_id, unit_id, attempt) = (%s, %s, %s)",
        *key,
    )
    assert got == [
        ("4g", T0, started, finished, "succeeded", "c014.af.uchicago.edu", "4417203.12", "two electrons", None)
    ]

    # Verification comes later, once
    verified = r.AttemptRecord(*key, verification="accepted", verify_reasons=[], events=30_000, sum_of_weights=1.5e7)
    await rep.write_attempt(conn, verified)
    await rep.write_attempt(conn, replace(verified, verification="rejected", verify_reasons=["size mismatch"]))
    assert await rows(
        conn,
        "SELECT verification, verify_reasons, events FROM production.attempts"
        " WHERE (production_id, unit_id, attempt) = (%s, %s, %s)",
        *key,
    ) == [("accepted", [], 30_000)]

    long = r.AttemptRecord(p.production_id, unit, 2, message="x" * 10_000)
    await rep.write_attempt(conn, long)
    ((message,),) = await rows(conn, "SELECT message FROM production.attempts WHERE attempt = 2")
    assert len(message) == r.MESSAGE_MAX


async def outcomes(conn: Conn, production_id: str, guid: str) -> list[tuple]:
    return await rows(
        conn,
        "SELECT rse, last_outcome, last_error FROM production.file_replicas WHERE production_id = %s AND guid = %s"
        " ORDER BY rank",
        production_id,
        guid,
    )


async def test_attempt_records_replica_reads(conn: Conn) -> None:
    p = await planned(conn)
    unit = p.units[0].unit_id
    t1 = T0 + timedelta(hours=1)
    reads = [ReplicaRead("g000", f"{BNL}/0", failed=[FailedRead(f"{MWT2}/0", "[3010] no such file")])]
    unreadable = [UnreadableFile("g001", [FailedRead(f"{MWT2}/1", "timeout"), FailedRead(f"{BNL}/1", "timeout")])]
    await rep.write_attempt(conn, r.AttemptRecord(p.production_id, unit, 1, finished_at=t1, reads=reads))
    await rep.write_attempt(conn, r.AttemptRecord(p.production_id, unit, 2, finished_at=t1, unreadable=unreadable))
    assert await outcomes(conn, p.production_id, "g000") == [
        ("MWT2_DATADISK", "failed", "[3010] no such file"),
        ("BNL-OSG2_DATADISK", "ok", None),
    ]
    assert await outcomes(conn, p.production_id, "g001") == [
        ("MWT2_DATADISK", "failed", "timeout"),
        ("BNL-OSG2_DATADISK", "failed", "timeout"),
    ]
    without = await rows(
        conn, "SELECT guid FROM production.files_without_replica WHERE production_id = %s", p.production_id
    )
    assert sorted(without) == [("g001",), ("g006",), ("g007",)]

    # A refresh after the reads replaces those replicas; reads from before it do not touch the new ones
    t2 = T0 + timedelta(hours=2)
    await rep.replace_replicas(
        conn, r.ReplicaRefresh(p.production_id, DID, t2, [r.FileReplicas("g001", [r.Replica("AGLT2_DATADISK", "x")])])
    )
    late = [UnreadableFile("g001", [FailedRead("x", "timeout")])]
    await rep.write_attempt(conn, r.AttemptRecord(p.production_id, unit, 3, finished_at=t1, unreadable=late))
    assert await outcomes(conn, p.production_id, "g001") == [("AGLT2_DATADISK", None, None)]
    # An older read does not overwrite a newer one
    await rep.write_attempt(
        conn, r.AttemptRecord(p.production_id, unit, 4, finished_at=T0, reads=[ReplicaRead("g000", f"{MWT2}/0")])
    )
    assert (await outcomes(conn, p.production_id, "g000"))[0] == ("MWT2_DATADISK", "failed", "[3010] no such file")


# --- decisions, datasets, productions ----------------------------------------------------------

EXCLUDE = Proposal(
    Action.EXCLUDE_FILES, "corrupt-tfile", "corrupt input", guids=["g005"], matched="Error in <TFile::Init>"
)


async def test_decision_written_once_outcome_later(conn: Conn) -> None:
    p = await planned(conn)
    rec = r.DecisionRecord("d1", p.production_id, EXCLUDE, "approved", "user:bengal1", T0, did=DID, unit_id="u")
    await rep.write_decision(conn, rec)
    await rep.write_decision(conn, replace(rec, verdict="rejected", decided_by="user:mallory"))
    await rep.write_decision(conn, replace(rec, outcome="excluded 1 file", outcome_at=T0 + timedelta(minutes=1)))
    await rep.write_decision(conn, replace(rec, outcome="other", outcome_at=T0 + timedelta(minutes=2)))
    assert await rows(
        conn, "SELECT action, verdict, decided_by, proposal->'guids', outcome, outcome_at FROM production.decisions"
    ) == [("exclude_files", "approved", "user:bengal1", ["g005"], "excluded 1 file", T0 + timedelta(minutes=1))]

    changed = replace(EXCLUDE, action=Action.ESCALATE)
    await rep.write_decision(conn, replace(rec, decision_id="d2", applied=changed))
    assert await rows(conn, "SELECT action FROM production.decisions WHERE decision_id = 'd2'") == [("escalate",)]

    other = await new_production(conn)
    with pytest.raises(rep.ReportingConflict, match="another production"):
        await rep.write_decision(conn, replace(rec, production_id=other))


async def test_dataset_and_production_apply_only_a_larger_seq(conn: Conn) -> None:
    p = await planned(conn)
    done = r.DatasetRecord(p.production_id, DID, "COMPLETE", 5, T0, 60_000, 3.0e7)
    await rep.write_dataset(conn, done)
    await rep.write_dataset(conn, r.DatasetRecord(p.production_id, DID, "RUNNING", 4))
    await rep.write_dataset(conn, replace(done, events_done=1))
    assert await rows(
        conn,
        "SELECT state, completed_at, events_done, seq FROM production.datasets WHERE production_id = %s",
        p.production_id,
    ) == [("COMPLETE", T0, 60_000, 5)]

    def prod(state: str, seq: int, **kw: Any) -> r.ProductionStateRecord:
        return r.ProductionStateRecord(p.production_id, state, seq, **kw)

    await rep.write_production(conn, prod("AWAITING_LAUNCH", 2))
    await rep.write_production(conn, prod("RUNNING", 3, launch_approved_by="bengal1", launch_approved_at=T0))
    await rep.write_production(conn, prod("SMOKE_TEST", 1))
    await rep.write_production(conn, prod("PAUSED", 4))
    assert await rows(
        conn,
        "SELECT state, launch_approved_by, launch_approved_at, seq FROM production.productions"
        " WHERE production_id = %s",
        p.production_id,
    ) == [("PAUSED", "bengal1", T0, 4)]
    with pytest.raises(rep.ReportingConflict):
        await rep.write_production(conn, replace(prod("RUNNING", 9), production_id="nosuch"))
    with pytest.raises(psycopg.errors.CheckViolation):
        await rep.write_production(conn, prod("SLEEPING", 10))


# --- invariants and what remains ---------------------------------------------------------------


@dataclass
class Scripted:
    """A dataset part way through: u0000 DONE (accepted, 2 hours), u0001 split by excluding g005
    (u0001.1 PLANNED, u0001.2 EXCLUDED with an approved decision), u0002 BLOCKED (tape files)."""

    production_id: str
    done: str
    kept: str
    excluded: str
    blocked: str


async def scripted(conn: Conn) -> Scripted:
    p = await planned(conn)
    u0, u1, u2 = p.units
    seq = 2
    for unit in (u0.start_attempt(), u0.start_attempt().with_state(S.RUNNING)):
        await rep.write_unit_state(
            conn, r.UnitStateRecord(p.production_id, unit.unit_id, unit.state, unit.attempt, unit.memclass, 0, seq)
        )
        seq += 1
    started, finished = T0 + timedelta(minutes=1), T0 + timedelta(minutes=1, hours=2)
    reads = [ReplicaRead(g, f"{MWT2}/{int(g[1:])}") for g in u0.guids]
    await rep.write_attempt(
        conn, r.attempt_from_manifest(p.production_id, manifest(u0.unit_id, 1, started, finished, reads))
    )
    await rep.write_attempt(conn, r.AttemptRecord(p.production_id, u0.unit_id, 1, verification="accepted"))
    await rep.write_unit_state(conn, r.UnitStateRecord(p.production_id, u0.unit_id, "DONE", 1, "4g", 0, seq))

    decision_id = f"dataset/{p.production_id}/{DID}/d1"
    decision = r.DecisionRecord(decision_id, p.production_id, EXCLUDE, "approved", "user:bengal1", T0, DID, u1.unit_id)
    await rep.write_decision(conn, decision)
    parent, (kept, excluded) = exclude_files(u1, ["g005"], p.files, PARAMS)
    await rep.write_units(
        conn,
        r.UnitsWrite(
            p.production_id,
            seq + 1,
            [r.UnitRecord.of(parent), r.UnitRecord.of(kept), r.UnitRecord.of(excluded, decision_id=decision_id)],
        ),
    )
    await rep.write_dataset(conn, r.DatasetRecord(p.production_id, DID, "RUNNING", seq + 2))
    return Scripted(p.production_id, u0.unit_id, kept.unit_id, excluded.unit_id, u2.unit_id)


CORRUPTIONS = {
    # g000 also in the blocked unit (events adjusted, so nothing else breaks)
    "file_not_in_one_live_unit": [
        "INSERT INTO production.unit_files (production_id, unit_id, did, guid, ordinal)"
        " VALUES (%(p)s, %(blocked)s, %(did)s, 'g000', 99)",
        "UPDATE production.units SET events_expected = events_expected + 10000"
        " WHERE production_id = %(p)s AND unit_id = %(blocked)s",
    ],
    "children_do_not_partition": [
        "UPDATE production.units SET parent_id = NULL WHERE production_id = %(p)s AND unit_id = %(excluded)s"
    ],
    "unit_events_mismatch": [
        "UPDATE production.units SET events_expected = 1 WHERE production_id = %(p)s AND unit_id = %(kept)s"
    ],
    "excluded_without_approval": ["UPDATE production.decisions SET verdict = 'rejected' WHERE production_id = %(p)s"],
    "complete_with_unfinished_units": ["UPDATE production.datasets SET state = 'COMPLETE' WHERE production_id = %(p)s"],
    "multiple_accepted_attempts": [
        "INSERT INTO production.attempts (production_id, unit_id, attempt, verification)"
        " VALUES (%(p)s, %(done)s, 2, 'accepted')"
    ],
    "done_without_accepted_attempt": [
        "UPDATE production.attempts SET verification = 'rejected' WHERE production_id = %(p)s"
    ],
}


async def test_scripted_dataset_holds_the_invariants(conn: Conn) -> None:
    s = await scripted(conn)
    report = await rep.invariant_violations(conn, r.InvariantsRequest(s.production_id))
    assert report == r.InvariantReport(s.production_id, 0, [])


@pytest.mark.parametrize("kind", sorted(CORRUPTIONS))
async def test_each_invariant_breach_is_reported(conn: Conn, kind: str) -> None:
    s = await scripted(conn)
    other = await scripted(conn)
    params = dataclasses.asdict(s) | {"p": s.production_id, "did": DID}
    for statement in CORRUPTIONS[kind]:
        await conn.execute(statement, params)
    assert await kinds(conn, s.production_id) == {kind}
    assert await kinds(conn, other.production_id) == set()


async def test_invariant_report_is_capped_and_filtered(conn: Conn) -> None:
    s = await scripted(conn)
    await conn.execute("DELETE FROM production.attempts WHERE production_id = %s", (s.production_id,))
    await conn.execute(
        "UPDATE production.units SET state = 'SUPERSEDED' WHERE production_id = %s AND unit_id = %s",
        (s.production_id, s.blocked),
    )
    full = await rep.invariant_violations(conn, r.InvariantsRequest(s.production_id))
    assert full.count == len(full.violations) == 5  # 2 files, their parent twice, 1 unit done without
    capped = await rep.invariant_violations(conn, r.InvariantsRequest(s.production_id, limit=2))
    assert (capped.count, capped.violations) == (5, full.violations[:2])
    other = await rep.invariant_violations(conn, r.InvariantsRequest(s.production_id, did="other:did"))
    assert other.count == 0


async def test_what_remains(conn: Conn) -> None:
    s = await scripted(conn)
    left = await rep.what_remains(conn, r.WhatRemainsRequest(s.production_id))
    # u0001.1 (g003, g004) and u0002 (g006, g007, no replica); one accepted attempt of 2 hours
    assert left == r.WhatRemains(
        production_id=s.production_id,
        units=2,
        files=4,
        events=40_000,
        core_hours=4.0,
        median_unit_seconds=7200.0,
        files_without_replica=2,
        by_state=[r.Remaining(DID, "BLOCKED", 1, 2, 20_000, 180), r.Remaining(DID, "PLANNED", 1, 2, 20_000, 180)],
        truncated=False,
    )
    capped = await rep.what_remains(conn, r.WhatRemainsRequest(s.production_id, limit=1))
    assert (capped.by_state, capped.truncated, capped.units) == (left.by_state[:1], True, 2)


async def test_what_remains_before_any_accepted_attempt(conn: Conn) -> None:
    p = await planned(conn)
    left = await rep.what_remains(conn, r.WhatRemainsRequest(p.production_id))
    assert (left.units, left.files, left.median_unit_seconds, left.files_without_replica) == (3, 8, None, 2)
    assert left.core_hours == sum(u.est_seconds for u in p.units) / 3600


# --- activities --------------------------------------------------------------------------------


async def test_activities(db: str) -> None:
    pool = await rep.open_pool(db)
    try:
        acts = rep.ReportingActivities(pool)
        assert {a.__temporal_activity_definition.name for a in acts.all()} == {  # type: ignore[attr-defined]
            r.RECORD_SNAPSHOT,
            r.RECORD_REPLICAS,
            r.RECORD_UNITS,
            r.RECORD_UNIT,
            r.RECORD_ATTEMPT,
            r.RECORD_DECISION,
            r.RECORD_DATASET,
            r.RECORD_PRODUCTION,
            r.CHECK_INVARIANTS,
            r.WHAT_REMAINS,
        }
        env = ActivityEnvironment()
        async with pool.connection() as conn:
            production_id = await new_production(conn)
        files = snapshot_files(3)
        await env.run(acts.record_snapshot, r.SnapshotPage(production_id, DID, T0, files))
        units = plan_units(DID, [f.planning_file() for f in files], PARAMS)
        await env.run(acts.record_units, r.UnitsWrite(production_id, 1, [r.UnitRecord.of(u) for u in units]))
        report = await env.run(acts.check_invariants, r.InvariantsRequest(production_id))
        assert report.count == 0
        left = await env.run(acts.what_remains, r.WhatRemainsRequest(production_id))
        assert (left.units, left.files) == (1, 3)

        stray = r.UnitRecord.of(replace(units[0], unit_id="x-u0001", guids=("nosuchguid",)))
        with pytest.raises(ApplicationError) as failure:
            await env.run(acts.record_units, r.UnitsWrite(production_id, 2, [stray]))
        assert (failure.value.type, failure.value.non_retryable) == (r.REPORTING_INTEGRITY, True)
        with pytest.raises(ApplicationError) as failure:
            await env.run(acts.record_unit, r.UnitStateRecord(production_id, "nosuchunit", "QUEUED", 1, "4g", 0, 2))
        assert (failure.value.type, failure.value.non_retryable) == (r.REPORTING_INTEGRITY, True)
    finally:
        await pool.close()


# --- payloads ----------------------------------------------------------------------------------

converter = DataConverter.default.payload_converter
EXAMPLE_MANIFEST = json.loads((Path(__file__).resolve().parents[1] / "contract-examples" / "manifest.json").read_text())


def payload_examples() -> list[Any]:
    m = converter.from_payload(converter.to_payload(EXAMPLE_MANIFEST), Manifest)
    return [
        r.SnapshotPage("p1", DID, T0, snapshot_files(2, tape=(1,))),
        r.ReplicaRefresh("p1", DID, T0, [r.FileReplicas("g000", [r.Replica("MWT2_DATADISK", "root://x")])]),
        r.UnitsWrite("p1", 3, [r.UnitRecord("u1", DID, ["g000"], 1, 2, 3, "4g", "PLANNED", parent_id="u")]),
        r.UnitStateRecord("p1", "u1", "QUEUED", 1, "8g", 2, 7, decision_id=None),
        r.attempt_from_manifest("p1", m, "/m.json"),
        r.AttemptRecord("p1", "u1", 1, unreadable=[UnreadableFile("g0", [FailedRead("root://x", "e")])], read_at=T0),
        r.DecisionRecord("d", "p1", EXCLUDE, "approved", "policy:first-memory", T0, applied=EXCLUDE, outcome_at=T0),
        r.DatasetRecord("p1", DID, "COMPLETE", 9, T0, 10, 1.5),
        r.ProductionStateRecord("p1", "RUNNING", 2, "bengal1", T0),
        r.InvariantsRequest("p1", DID, 5),
        r.InvariantReport("p1", 1, [r.Violation("unit_events_mismatch", "p1", DID, "u1", None, "x")]),
        r.WhatRemainsRequest("p1"),
        r.WhatRemains("p1", 1, 2, 3, 0.5, None, 0, [r.Remaining(DID, "PLANNED", 1, 2, 3, 4)], False),
    ]


@pytest.mark.parametrize("value", payload_examples(), ids=lambda v: type(v).__name__)
def test_payloads_round_trip(value: Any) -> None:
    assert converter.from_payload(converter.to_payload(value), type(value)) == value
