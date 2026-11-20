"""What workflows record in the reporting schema: activity names and their payloads.

Pure: stdlib and the package's pure modules only, deterministic, no I/O, so workflow code can
import it. reporting.py writes these to Postgres (migrations/0001_reporting.sql) in controller
activities.

Every write is idempotent, because activities retry:

- units, datasets and productions carry seq, a counter the writing workflow increments with every
  write (from 1, carried across continue-as-new). A row changes only for a larger seq: a retry
  (the same seq) does nothing, and a try that timed out but committed late (a smaller seq) cannot
  undo a newer write.
- attempts: every field is written once; the first non-null value recorded stays.
- decisions are written once; only the outcome is filled in later, once.
- snapshot files are written once; replicas are replaced by a newer resolution only.

Payloads are JSON-shaped for Temporal's default converter: datetimes (timezone-aware, from
workflow.now() or the activity that observed them) as ISO 8601, no durations.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from agentic_ops_worker.contracts import Manifest, ManifestStatus, ReplicaRead, UnreadableFile, VerifyResult
from agentic_ops_worker.planning import SnapshotFile
from agentic_ops_worker.signatures import Proposal
from agentic_ops_worker.units import Unit

# Activity names, on the controller queue
RECORD_SNAPSHOT = "record_snapshot"
RECORD_REPLICAS = "record_replicas"
RECORD_UNITS = "record_units"
RECORD_UNIT = "record_unit"
RECORD_ATTEMPT = "record_attempt"
RECORD_DECISION = "record_decision"
RECORD_DATASET = "record_dataset"
RECORD_PRODUCTION = "record_production"
CHECK_INVARIANTS = "check_invariants"
WHAT_REMAINS = "what_remains"

# ApplicationError type of a write the schema refuses (a unit listing a file outside the snapshot,
# a unit or production never recorded, a unit's files changed). Non-retryable: retrying cannot fix
# it, it is a bug in the caller.
REPORTING_INTEGRITY = "ReportingIntegrity"

# Longest error message kept on an attempt; the rest is in the log
MESSAGE_MAX = 4000


class ProductionState(StrEnum):
    CREATED = "CREATED"
    CHECKING_BUILD = "CHECKING_BUILD"
    SMOKE_TEST = "SMOKE_TEST"
    AWAITING_LAUNCH = "AWAITING_LAUNCH"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class DatasetState(StrEnum):
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"


class AttemptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # Heartbeat or start-to-close timeout: eviction, lost worker or node
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class Verification(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    # Verified after another attempt of the unit was accepted; cleaned up
    DUPLICATE = "duplicate"


class Verdict(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


# --- snapshot and replicas ---------------------------------------------------------------------


@dataclass
class Replica:
    rse: str
    # root:// PFN without the XCache prefix
    pfn: str


@dataclass
class SnapshotFileRecord:
    guid: str
    lfn: str
    size: int
    checksum: str
    events: int
    # Disk replicas in preference order; empty when the file has none (tape only, lost)
    replicas: list[Replica] = field(default_factory=list)

    def planning_file(self) -> SnapshotFile:
        return SnapshotFile(
            self.guid, self.lfn, self.size, self.events, self.replicas[0].rse if self.replicas else None
        )


@dataclass
class SnapshotPage:
    """Some of a dataset's snapshot files. A large container is recorded a page at a time; each
    file is written once, so pages may repeat."""

    production_id: str
    did: str
    snapshot_at: datetime
    files: list[SnapshotFileRecord]


@dataclass
class FileReplicas:
    guid: str
    replicas: list[Replica]


@dataclass
class ReplicaRefresh:
    """refresh_replicas' result: these files' replicas as resolved at resolved_at. Replaces the
    recorded replicas of each file unless they were resolved at or after resolved_at."""

    production_id: str
    did: str
    resolved_at: datetime
    files: list[FileReplicas]


# --- units -------------------------------------------------------------------------------------


@dataclass
class UnitRecord:
    """A unit with its files, as planning.py makes it."""

    unit_id: str
    did: str
    guids: list[str]
    size: int
    events_expected: int
    est_seconds: int
    memclass: str
    state: str  # UnitState
    attempt: int = 0
    failures: int = 0
    parent_id: str | None = None
    decision_id: str | None = None

    @classmethod
    def of(cls, unit: Unit, failures: int = 0, decision_id: str | None = None) -> "UnitRecord":
        return cls(
            unit_id=unit.unit_id,
            did=unit.did,
            guids=list(unit.guids),
            size=unit.size,
            events_expected=unit.events_expected,
            est_seconds=unit.est_seconds,
            memclass=unit.memclass,
            state=unit.state.value,
            attempt=unit.attempt,
            failures=failures,
            parent_id=unit.parent_id,
            decision_id=decision_id,
        )


@dataclass
class UnitsWrite:
    """Units written together in one transaction: a split's parent and children, an exclusion,
    an unblock. Whole plans are written by the plan_units activity itself, not sent through
    history."""

    production_id: str
    seq: int
    units: list[UnitRecord]


@dataclass
class UnitStateRecord:
    """A unit's state after a transition. decision_id, when given, is the approved decision that
    excluded it."""

    production_id: str
    unit_id: str
    state: str  # UnitState
    attempt: int
    memclass: str
    failures: int
    seq: int
    decision_id: str | None = None


# --- attempts ----------------------------------------------------------------------------------


@dataclass
class AttemptRecord:
    """What is known of an attempt so far. Fields left None are not written; fields already
    recorded keep their first value."""

    production_id: str
    unit_id: str
    attempt: int
    memclass: str | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    condor_job: str | None = None
    host: str | None = None
    outcome: str | None = None  # AttemptOutcome
    error_type: str | None = None
    failure_class: str | None = None  # failures.FailureClass
    message: str | None = None
    output_path: str | None = None
    manifest_path: str | None = None
    log_path: str | None = None
    manifest: Manifest | None = None
    verification: str | None = None  # Verification
    verify_reasons: list[str] | None = None
    events: int | None = None
    sum_of_weights: float | None = None
    cleaned_up_at: datetime | None = None
    # Replica reads to record on file_replicas: from the manifest, and from InputUnreadableDetails
    reads: list[ReplicaRead] = field(default_factory=list)
    unreadable: list[UnreadableFile] = field(default_factory=list)
    # When the reads happened, for replicas resolved before then; defaults to finished_at
    read_at: datetime | None = None


def attempt_from_manifest(production_id: str, manifest: Manifest, manifest_path: str | None = None) -> AttemptRecord:
    """An attempt as run_unit reported it."""
    return AttemptRecord(
        production_id=production_id,
        unit_id=manifest.unit_id,
        attempt=manifest.attempt,
        started_at=manifest.started,
        finished_at=manifest.finished,
        condor_job=manifest.condor_job,
        host=manifest.host,
        outcome=AttemptOutcome.SUCCEEDED if manifest.status == ManifestStatus.SUCCEEDED else AttemptOutcome.FAILED,
        output_path=manifest.output_path,
        manifest_path=manifest_path,
        log_path=manifest.log_path,
        manifest=manifest,
        reads=list(manifest.replicas),
    )


def attempt_from_verify(production_id: str, result: VerifyResult, duplicate: bool = False) -> AttemptRecord:
    """An attempt as verify_unit found it. duplicate: another attempt was accepted first."""
    if duplicate:
        verification = Verification.DUPLICATE
    else:
        verification = Verification.ACCEPTED if result.accepted else Verification.REJECTED
    return AttemptRecord(
        production_id=production_id,
        unit_id=result.unit_id,
        attempt=result.attempt,
        verification=verification,
        verify_reasons=list(result.reasons),
        events=result.events,
        sum_of_weights=result.sum_of_weights,
    )


# --- decisions, datasets, productions ----------------------------------------------------------


@dataclass
class DecisionRecord:
    """A decision on a proposal. decision_id is unique across productions and deterministic in
    the deciding workflow (its workflow ID and a counter), so a retry writes the same row.
    Recording it again with an outcome fills the outcome in."""

    decision_id: str
    production_id: str
    proposal: Proposal
    verdict: str  # Verdict
    decided_by: str
    decided_at: datetime
    did: str | None = None
    unit_id: str | None = None
    fingerprint: str | None = None
    # The proposal as applied, when the decider changed it
    applied: Proposal | None = None
    outcome: str | None = None
    outcome_at: datetime | None = None

    @property
    def action(self) -> str:
        return (self.applied or self.proposal).action.value


@dataclass
class DatasetRecord:
    production_id: str
    did: str
    state: str  # DatasetState
    seq: int
    completed_at: datetime | None = None
    events_done: int | None = None
    sum_of_weights: float | None = None


@dataclass
class ProductionStateRecord:
    production_id: str
    state: str  # ProductionState
    seq: int
    launch_approved_by: str | None = None
    launch_approved_at: datetime | None = None


# --- reads -------------------------------------------------------------------------------------


@dataclass
class InvariantsRequest:
    production_id: str
    did: str | None = None
    # Violations returned; the count covers all of them
    limit: int = 100


@dataclass
class Violation:
    kind: str
    production_id: str
    did: str | None
    unit_id: str | None
    guid: str | None
    detail: str


@dataclass
class InvariantReport:
    production_id: str
    count: int
    violations: list[Violation]


@dataclass
class WhatRemainsRequest:
    production_id: str
    # Rows of by_state returned
    limit: int = 200


@dataclass
class Remaining:
    did: str
    state: str
    units: int
    files: int
    events: int
    est_seconds: int


@dataclass
class WhatRemains:
    production_id: str
    units: int
    files: int
    events: int
    # Units left times the median wall time of accepted attempts; the planning estimate before
    # any attempt is accepted
    core_hours: float
    median_unit_seconds: float | None
    # Files of unfinished units with no replica that has not failed: only a person resolves them
    files_without_replica: int
    by_state: list[Remaining]
    truncated: bool
