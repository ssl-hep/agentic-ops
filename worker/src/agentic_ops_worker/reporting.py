"""Writes to and reads from the reporting schema, and the controller activities that do them.

The schema is migrations/0001_reporting.sql; the payloads and the idempotency rules are in
records.py. Each write function runs in one transaction on the connection it is given, and is safe
to run any number of times. ReportingActivities wraps them as activities on a connection pool.

Not imported by workflow code: it needs psycopg and does I/O.
"""

import dataclasses
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from temporalio import activity
from temporalio.exceptions import ApplicationError

from agentic_ops_worker import records as r
from agentic_ops_worker.names import check_production_id

Conn = psycopg.AsyncConnection


class ReportingConflict(Exception):
    """A write the schema's constraints do not catch but that cannot be right: a unit or
    production never recorded, a unit whose files changed, a file outside the snapshot."""


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot encode {type(value).__name__} as JSON")


def _jsonb(value: Any) -> Jsonb | None:
    if value is None:
        return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    return Jsonb(value, dumps=lambda v: json.dumps(v, default=_default))


def _clip(text: str | None) -> str | None:
    if text is None or len(text) <= r.MESSAGE_MAX:
        return text
    return text[: r.MESSAGE_MAX - 1] + "…"


# --- selections and productions (the backend creates these; tests use them too) -----------------


@dataclass
class Selection:
    selection_id: str
    analysis: str
    content_hash: str
    created_by: str
    dids: list[str]
    approved_by: str | None = None
    approved_at: datetime | None = None
    totals: dict[str, float] = field(default_factory=dict)


@dataclass
class Production:
    production_id: str
    analysis: str
    config_name: str
    clone_path: str
    commit_sha: str
    release: str
    tarball_path: str
    tarball_digest: str
    tag: str
    selection_id: str
    owner: str
    diff_digest: str | None = None


async def create_selection(conn: Conn, sel: Selection) -> None:
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO production.selections"
            " (selection_id, analysis, content_hash, created_by, approved_by, approved_at, totals)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (
                sel.selection_id,
                sel.analysis,
                sel.content_hash,
                sel.created_by,
                sel.approved_by,
                sel.approved_at,
                _jsonb(sel.totals),
            ),
        )
        if cur.rowcount:
            async with conn.cursor() as c:
                await c.executemany(
                    "INSERT INTO production.selection_dids (selection_id, did, ordinal) VALUES (%s, %s, %s)",
                    [(sel.selection_id, did, i) for i, did in enumerate(sel.dids)],
                )


async def create_production(conn: Conn, prod: Production) -> None:
    check_production_id(prod.production_id)
    fields = dataclasses.asdict(prod)
    columns = ", ".join(fields)
    values = ", ".join(f"%({name})s" for name in fields)
    await conn.execute(
        f"INSERT INTO production.productions ({columns}) VALUES ({values}) ON CONFLICT DO NOTHING".encode(), fields
    )


# --- snapshot and replicas ---------------------------------------------------------------------


async def write_snapshot(conn: Conn, page: r.SnapshotPage) -> None:
    """Record a page of a dataset's snapshot, and the dataset itself if it is new."""
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO production.datasets (production_id, did, state, snapshot_at)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT (production_id, did) DO UPDATE"
            " SET snapshot_at = coalesce(datasets.snapshot_at, EXCLUDED.snapshot_at)",
            (page.production_id, page.did, r.DatasetState.PLANNING.value, page.snapshot_at),
        )
        key = (page.production_id, page.did)
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO production.dataset_snapshots"
                " (production_id, did, guid, lfn, size, checksum, events, snapshot_at, replicas_resolved_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                [
                    (*key, f.guid, f.lfn, f.size, f.checksum, f.events, page.snapshot_at, page.snapshot_at)
                    for f in page.files
                ],
            )
            # Only while the file's replicas are still the snapshot's: a page repeated after a
            # refresh must not bring old replicas back
            await cur.executemany(
                "INSERT INTO production.file_replicas"
                " (production_id, did, guid, rse, pfn, rank, resolved_at)"
                " SELECT %(p)s, %(d)s, %(g)s, %(rse)s, %(pfn)s, %(rank)s, %(at)s"
                " WHERE EXISTS (SELECT 1 FROM production.dataset_snapshots"
                "   WHERE production_id = %(p)s AND did = %(d)s AND guid = %(g)s AND replicas_resolved_at = %(at)s)"
                " ON CONFLICT DO NOTHING",
                [
                    {"p": page.production_id, "d": page.did, "g": f.guid, "rse": rep.rse, "pfn": rep.pfn, "rank": i}
                    | {"at": page.snapshot_at}
                    for f in page.files
                    for i, rep in enumerate(f.replicas)
                ],
            )


async def replace_replicas(conn: Conn, refresh: r.ReplicaRefresh) -> None:
    """Replace the replicas of the refreshed files, unless they were resolved at or after this
    refresh (a retry, or an older refresh committing late)."""
    guids = [f.guid for f in refresh.files]
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT guid FROM production.dataset_snapshots WHERE production_id = %s AND did = %s AND guid = ANY(%s)",
            (refresh.production_id, refresh.did, guids),
        )
        unknown = set(guids) - {row[0] for row in await cur.fetchall()}
        if unknown:
            raise ReportingConflict(f"files not in the snapshot of {refresh.did}: {', '.join(sorted(unknown))}")
        cur = await conn.execute(
            "UPDATE production.dataset_snapshots SET replicas_resolved_at = %s"
            " WHERE production_id = %s AND did = %s AND guid = ANY(%s) AND replicas_resolved_at < %s RETURNING guid",
            (refresh.resolved_at, refresh.production_id, refresh.did, guids, refresh.resolved_at),
        )
        fresh = {row[0] for row in await cur.fetchall()}
        if not fresh:
            return
        await conn.execute(
            "DELETE FROM production.file_replicas WHERE production_id = %s AND did = %s AND guid = ANY(%s)",
            (refresh.production_id, refresh.did, list(fresh)),
        )
        async with conn.cursor() as c:
            await c.executemany(
                "INSERT INTO production.file_replicas (production_id, did, guid, rse, pfn, rank, resolved_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [
                    (refresh.production_id, refresh.did, f.guid, rep.rse, rep.pfn, i, refresh.resolved_at)
                    for f in refresh.files
                    if f.guid in fresh
                    for i, rep in enumerate(f.replicas)
                ],
            )


# --- units -------------------------------------------------------------------------------------

_UPSERT_UNIT = """
INSERT INTO production.units (production_id, unit_id, did, state, attempt, failures, memclass, size,
                              events_expected, est_seconds, parent_id, decision_id, seq)
VALUES (%(production_id)s, %(unit_id)s, %(did)s, %(state)s, %(attempt)s, %(failures)s, %(memclass)s,
        %(size)s, %(events_expected)s, %(est_seconds)s, %(parent_id)s, %(decision_id)s, %(seq)s)
ON CONFLICT (production_id, unit_id) DO UPDATE SET
    state = EXCLUDED.state, attempt = EXCLUDED.attempt, failures = EXCLUDED.failures,
    memclass = EXCLUDED.memclass, size = EXCLUDED.size, events_expected = EXCLUDED.events_expected,
    est_seconds = EXCLUDED.est_seconds, decision_id = coalesce(EXCLUDED.decision_id, units.decision_id),
    seq = EXCLUDED.seq, updated_at = now()
WHERE units.seq < EXCLUDED.seq
"""


async def write_units(conn: Conn, write: r.UnitsWrite) -> None:
    """Record units with their files, all or none. A unit's files are written once; recording it
    again with other files is a conflict."""
    if write.seq < 1:
        raise ValueError("seq starts at 1")
    async with conn.transaction():
        for unit in write.units:
            params = dataclasses.asdict(unit) | {"production_id": write.production_id, "seq": write.seq}
            del params["guids"]
            await conn.execute(_UPSERT_UNIT, params)
            cur = await conn.execute(
                "SELECT guid FROM production.unit_files WHERE production_id = %s AND unit_id = %s ORDER BY ordinal",
                (write.production_id, unit.unit_id),
            )
            existing = [row[0] for row in await cur.fetchall()]
            if not existing:
                async with conn.cursor() as c:
                    await c.executemany(
                        "INSERT INTO production.unit_files (production_id, unit_id, did, guid, ordinal)"
                        " VALUES (%s, %s, %s, %s, %s)",
                        [(write.production_id, unit.unit_id, unit.did, g, i) for i, g in enumerate(unit.guids)],
                    )
            elif existing != unit.guids:
                raise ReportingConflict(f"unit {unit.unit_id} was recorded with other files")


async def _require_unit(conn: Conn, production_id: str, unit_id: str) -> None:
    cur = await conn.execute(
        "SELECT 1 FROM production.units WHERE production_id = %s AND unit_id = %s", (production_id, unit_id)
    )
    if not await cur.fetchone():
        raise ReportingConflict(f"unit {unit_id} of {production_id} was never recorded")


async def write_unit_state(conn: Conn, rec: r.UnitStateRecord) -> None:
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE production.units SET state = %s, attempt = %s, memclass = %s, failures = %s,"
            " decision_id = coalesce(%s, decision_id), seq = %s, updated_at = now()"
            " WHERE production_id = %s AND unit_id = %s AND seq < %s",
            (
                rec.state,
                rec.attempt,
                rec.memclass,
                rec.failures,
                rec.decision_id,
                rec.seq,
                rec.production_id,
                rec.unit_id,
                rec.seq,
            ),
        )
        if not cur.rowcount:
            await _require_unit(conn, rec.production_id, rec.unit_id)


# --- attempts ----------------------------------------------------------------------------------

_ATTEMPT_FIELDS = [
    "memclass",
    "queued_at",
    "started_at",
    "finished_at",
    "condor_job",
    "host",
    "outcome",
    "error_type",
    "failure_class",
    "message",
    "output_path",
    "manifest_path",
    "log_path",
    "manifest",
    "verification",
    "verify_reasons",
    "events",
    "sum_of_weights",
    "cleaned_up_at",
]
_UPSERT_ATTEMPT = (
    "INSERT INTO production.attempts (production_id, unit_id, attempt, "
    + ", ".join(_ATTEMPT_FIELDS)
    + ") VALUES (%(production_id)s, %(unit_id)s, %(attempt)s, "
    + ", ".join(f"%({name})s" for name in _ATTEMPT_FIELDS)
    + ") ON CONFLICT (production_id, unit_id, attempt) DO UPDATE SET "
    + ", ".join(f"{name} = coalesce(attempts.{name}, EXCLUDED.{name})" for name in _ATTEMPT_FIELDS)
)

_RECORD_READ = """
UPDATE production.file_replicas SET last_outcome = %(outcome)s, last_error = %(error)s, last_read_at = %(at)s
WHERE production_id = %(production_id)s AND did = %(did)s AND guid = %(guid)s AND pfn = %(pfn)s
  AND resolved_at <= %(at)s AND (last_read_at IS NULL OR last_read_at <= %(at)s)
"""


async def write_attempt(conn: Conn, rec: r.AttemptRecord) -> None:
    """Record what is known of an attempt (each field once), and the replica reads it reports on
    file_replicas: the last outcome of each replica, unless a refresh replaced it since."""
    params = {name: getattr(rec, name) for name in _ATTEMPT_FIELDS}
    params |= {
        "production_id": rec.production_id,
        "unit_id": rec.unit_id,
        "attempt": rec.attempt,
        "message": _clip(rec.message),
        "manifest": _jsonb(rec.manifest),
        "verify_reasons": _jsonb(rec.verify_reasons),
    }
    async with conn.transaction():
        await conn.execute(_UPSERT_ATTEMPT.encode(), params)
        reads = [(read.guid, read.pfn, "ok", None) for read in rec.reads]
        reads += [(read.guid, f.pfn, "failed", f.error) for read in rec.reads for f in read.failed]
        reads += [(u.guid, f.pfn, "failed", f.error) for u in rec.unreadable for f in u.failed]
        if not reads:
            return
        cur = await conn.execute(
            "SELECT did, now() FROM production.units WHERE production_id = %s AND unit_id = %s",
            (rec.production_id, rec.unit_id),
        )
        did, now = await cur.fetchone()  # the unit exists: the attempt's foreign key says so
        at = rec.read_at or rec.finished_at or now
        async with conn.cursor() as c:
            await c.executemany(
                _RECORD_READ,
                [
                    {"production_id": rec.production_id, "did": did, "guid": guid, "pfn": pfn}
                    | {"outcome": outcome, "error": _clip(error), "at": at}
                    for guid, pfn, outcome, error in reads
                ],
            )


# --- decisions, datasets, productions ----------------------------------------------------------


async def write_decision(conn: Conn, rec: r.DecisionRecord) -> None:
    cur = await conn.execute(
        "INSERT INTO production.decisions (decision_id, production_id, did, unit_id, fingerprint, proposal,"
        " applied, action, verdict, decided_by, decided_at, outcome, outcome_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (decision_id) DO UPDATE SET outcome = coalesce(decisions.outcome, EXCLUDED.outcome),"
        " outcome_at = coalesce(decisions.outcome_at, EXCLUDED.outcome_at)"
        " WHERE decisions.production_id = EXCLUDED.production_id",
        (
            rec.decision_id,
            rec.production_id,
            rec.did,
            rec.unit_id,
            rec.fingerprint,
            _jsonb(rec.proposal),
            _jsonb(rec.applied),
            rec.action,
            rec.verdict,
            rec.decided_by,
            rec.decided_at,
            rec.outcome,
            rec.outcome_at,
        ),
    )
    if not cur.rowcount:
        raise ReportingConflict(f"decision {rec.decision_id} was recorded for another production")


async def write_dataset(conn: Conn, rec: r.DatasetRecord) -> None:
    if rec.seq < 1:
        raise ValueError("seq starts at 1")
    await conn.execute(
        "INSERT INTO production.datasets (production_id, did, state, completed_at, events_done, sum_of_weights, seq)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (production_id, did) DO UPDATE SET"
        " state = EXCLUDED.state, completed_at = coalesce(EXCLUDED.completed_at, datasets.completed_at),"
        " events_done = coalesce(EXCLUDED.events_done, datasets.events_done),"
        " sum_of_weights = coalesce(EXCLUDED.sum_of_weights, datasets.sum_of_weights),"
        " seq = EXCLUDED.seq, updated_at = now()"
        " WHERE datasets.seq < EXCLUDED.seq",
        (rec.production_id, rec.did, rec.state, rec.completed_at, rec.events_done, rec.sum_of_weights, rec.seq),
    )


async def write_production(conn: Conn, rec: r.ProductionStateRecord) -> None:
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE production.productions SET state = %s,"
            " launch_approved_by = coalesce(%s, launch_approved_by),"
            " launch_approved_at = coalesce(%s, launch_approved_at), seq = %s, updated_at = now()"
            " WHERE production_id = %s AND seq < %s",
            (rec.state, rec.launch_approved_by, rec.launch_approved_at, rec.seq, rec.production_id, rec.seq),
        )
        if not cur.rowcount:
            cur = await conn.execute(
                "SELECT 1 FROM production.productions WHERE production_id = %s", (rec.production_id,)
            )
            if not await cur.fetchone():
                raise ReportingConflict(f"production {rec.production_id} was never recorded")


# --- reads -------------------------------------------------------------------------------------


async def invariant_violations(conn: Conn, req: r.InvariantsRequest) -> r.InvariantReport:
    cur = await conn.execute(
        "SELECT kind, production_id, did, unit_id, guid, detail, count(*) OVER ()"
        " FROM production.invariant_violations"
        " WHERE production_id = %(p)s AND (%(d)s::text IS NULL OR did = %(d)s)"
        " ORDER BY kind, did, unit_id, guid LIMIT %(n)s",
        {"p": req.production_id, "d": req.did, "n": req.limit},
    )
    rows = await cur.fetchall()
    return r.InvariantReport(
        production_id=req.production_id,
        count=rows[0][6] if rows else 0,
        violations=[r.Violation(*row[:6]) for row in rows],
    )


async def what_remains(conn: Conn, req: r.WhatRemainsRequest) -> r.WhatRemains:
    cur = await conn.execute(
        "SELECT coalesce(sum(units), 0)::bigint, coalesce(sum(files), 0)::bigint, coalesce(sum(events), 0)::bigint,"
        " coalesce(sum(est_seconds), 0)::bigint,"
        " (SELECT median_seconds FROM production.unit_runtimes WHERE production_id = %(p)s),"
        " (SELECT count(*) FROM production.files_without_replica WHERE production_id = %(p)s)"
        " FROM production.what_remains WHERE production_id = %(p)s",
        {"p": req.production_id},
    )
    units, files, events, est_seconds, median, without_replica = await cur.fetchone()
    cur = await conn.execute(
        "SELECT did, state, units, files, events, est_seconds FROM production.what_remains"
        " WHERE production_id = %s ORDER BY did, state LIMIT %s",
        (req.production_id, req.limit + 1),
    )
    rows = await cur.fetchall()
    seconds = units * median if median is not None else est_seconds
    return r.WhatRemains(
        production_id=req.production_id,
        units=units,
        files=files,
        events=events,
        core_hours=seconds / 3600,
        median_unit_seconds=median,
        files_without_replica=without_replica,
        by_state=[r.Remaining(*row) for row in rows[: req.limit]],
        truncated=len(rows) > req.limit,
    )


# --- activities --------------------------------------------------------------------------------


async def open_pool(dsn: str, min_size: int = 1, max_size: int = 4) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(dsn, min_size=min_size, max_size=max_size, kwargs={"autocommit": True}, open=False)
    await pool.open(wait=True)
    return pool


class ReportingActivities:
    """The reporting activities, on the controller queue, over a pool of autocommit connections.

    A write the schema refuses fails as a non-retryable ReportingIntegrity error; any other
    database error is retried by Temporal.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def _run[P, T](self, fn: Callable[[Conn, P], Awaitable[T]], payload: P) -> T:
        try:
            async with self.pool.connection() as conn:
                return await fn(conn, payload)
        except (psycopg.IntegrityError, ReportingConflict) as e:
            raise ApplicationError(str(e), type=r.REPORTING_INTEGRITY, non_retryable=True) from e

    @activity.defn(name=r.RECORD_SNAPSHOT)
    async def record_snapshot(self, page: r.SnapshotPage) -> None:
        await self._run(write_snapshot, page)

    @activity.defn(name=r.RECORD_REPLICAS)
    async def record_replicas(self, refresh: r.ReplicaRefresh) -> None:
        await self._run(replace_replicas, refresh)

    @activity.defn(name=r.RECORD_UNITS)
    async def record_units(self, write: r.UnitsWrite) -> None:
        await self._run(write_units, write)

    @activity.defn(name=r.RECORD_UNIT)
    async def record_unit(self, rec: r.UnitStateRecord) -> None:
        await self._run(write_unit_state, rec)

    @activity.defn(name=r.RECORD_ATTEMPT)
    async def record_attempt(self, rec: r.AttemptRecord) -> None:
        await self._run(write_attempt, rec)

    @activity.defn(name=r.RECORD_DECISION)
    async def record_decision(self, rec: r.DecisionRecord) -> None:
        await self._run(write_decision, rec)

    @activity.defn(name=r.RECORD_DATASET)
    async def record_dataset(self, rec: r.DatasetRecord) -> None:
        await self._run(write_dataset, rec)

    @activity.defn(name=r.RECORD_PRODUCTION)
    async def record_production(self, rec: r.ProductionStateRecord) -> None:
        await self._run(write_production, rec)

    @activity.defn(name=r.CHECK_INVARIANTS)
    async def check_invariants(self, req: r.InvariantsRequest) -> r.InvariantReport:
        return await self._run(invariant_violations, req)

    @activity.defn(name=r.WHAT_REMAINS)
    async def what_remains(self, req: r.WhatRemainsRequest) -> r.WhatRemains:
        return await self._run(what_remains, req)

    def all(self) -> list[Callable[..., Awaitable[Any]]]:
        return [
            self.record_snapshot,
            self.record_replicas,
            self.record_units,
            self.record_unit,
            self.record_attempt,
            self.record_decision,
            self.record_dataset,
            self.record_production,
            self.check_invariants,
            self.what_remains,
        ]
