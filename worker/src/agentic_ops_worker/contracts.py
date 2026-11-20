"""Payloads shared with EWKit's worker code.

run_unit and the af-io activities are implemented in EWKit (phase 3: python/EWUnit.py,
share/EWWorker.py, share/EWAfIo.py); the workflows here call them by name. Neither side imports
the other's code: what they share is the JSON these dataclasses encode to with Temporal's default
data converter, which is field name for field name, datetimes as ISO 8601 strings.
worker/contract-examples/ holds one JSON file per payload, checked against these classes by
tests/test_contracts.py; EWKit's tests read the same files.

Rules for changing a payload while productions run: add fields with defaults, never rename or
remove one. The converter ignores fields it does not know, so an older reader survives a newer
writer.
"""

from dataclasses import dataclass, field
from datetime import datetime

# Activity names. Workflows schedule these by name on the queues in names.py.
RUN_UNIT = "run_unit"  # ewkit-<production>-<memclass>
CHECK_BUILD = "check_build"  # af-io
VERIFY_UNIT = "verify_unit"  # af-io
FETCH_LOG = "fetch_log"  # af-io
CLEANUP_ATTEMPT = "cleanup_attempt"  # af-io
WRITE_LISTS = "write_lists"  # af-io


class ErrorType:
    """ApplicationError types the EWKit activities raise (docs: Failure handling and triage).

    Temporal never retries run_unit (maximum_attempts=1): the dataset workflow handles every
    failure, and each retry is a new attempt with its own output path. Infrastructure failures
    (eviction, heartbeat timeout, node failure) raise nothing: the workflow sees the activity
    time out. NON_RETRYABLE matters for the af-io activities, which keep Temporal's retries.
    """

    # No candidate replica of an input file could be read. The workflow runs refresh_replicas,
    # then retries; after a few tries the unit is BLOCKED. details[0]: InputUnreadableDetails.
    INPUT_UNREADABLE = "InputUnreadable"
    # EWRun ran out of memory. Triage proposes the next memory class; past 16g, a split.
    OUT_OF_MEMORY = "OutOfMemory"
    # The unit hit the watchdog's hard time limit, or its output stopped growing. Handled like
    # OutOfMemory.
    WALLTIME = "Walltime"
    # An input file is bad (corrupt, unreadable on every replica in a way a retry will not fix).
    # Retried once; when the same GUID fails on a second host, triage proposes excluding it.
    # details[0]: BadInputDetails.
    BAD_INPUT = "BadInput"
    # EWRun or the worker crashed on its own code or config. Goes straight to triage.
    CODE_ERROR = "CodeError"
    # An af-io activity was given a path outside the production's roots. Non-retryable.
    PATH_OUTSIDE_ROOTS = "PathOutsideRoots"
    # check_build: the tarball's digest is not the one recorded on the production. Non-retryable.
    DIGEST_MISMATCH = "DigestMismatch"

    NON_RETRYABLE = (OUT_OF_MEMORY, WALLTIME, BAD_INPUT, CODE_ERROR, PATH_OUTSIDE_ROOTS, DIGEST_MISMATCH)


class ManifestStatus:
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# --- run_unit ----------------------------------------------------------------------------------


@dataclass
class InputFile:
    guid: str
    lfn: str
    # Candidate root:// PFNs in preference order, not yet prefixed with the XCache address; the
    # worker reads root://<xcache>//<pfn> and takes the first that answers.
    pfns: list[str]
    size: int
    events: int


@dataclass
class UnitSpec:
    """Input of run_unit."""

    unit_id: str
    attempt: int
    production_id: str
    analysis: str
    config_name: str
    tag: str
    did: str
    memclass: str
    files: list[InputFile]
    # Directories under the production's data root. The worker writes
    # <output_dir>/<unit_id>.a<attempt>.root and <manifest_dir>/<unit_id>.a<attempt>.json.
    output_dir: str
    manifest_dir: str
    log_dir: str
    events_expected: int
    is_mc: bool
    # EWRun -e: process at most this many events per unit (the smoke test). None for all.
    max_events: int | None = None


@dataclass
class UnitHeartbeat:
    """run_unit's heartbeat details, sent about once a minute."""

    events_processed: int
    # "staging" (copying and unpacking tarballs), "resolving" (statting replicas), "running"
    phase: str


@dataclass
class FailedRead:
    pfn: str
    error: str


@dataclass
class ReplicaRead:
    """Which replica of an input file was read, and the candidates that failed before it."""

    guid: str
    # Without the XCache prefix
    pfn: str
    failed: list[FailedRead] = field(default_factory=list)


@dataclass
class Cut:
    """One cutflow step. A cutflow is a list in cut order: the JSON encoder sorts object keys."""

    name: str
    events: int
    sum_of_weights: float


@dataclass
class Manifest:
    """Result of run_unit, also written as <manifest_dir>/<unit_id>.a<attempt>.json."""

    unit_id: str
    attempt: int
    status: str  # ManifestStatus
    exit_code: int
    events_processed: int
    sum_of_weights: float
    cutflow: list[Cut]
    output_path: str
    size: int
    # "sha256:<hex>"
    checksum: str
    replicas: list[ReplicaRead]
    host: str
    # HTCondor cluster.proc of the job that ran the unit
    condor_job: str
    started: datetime
    finished: datetime
    log_path: str


@dataclass
class UnreadableFile:
    guid: str
    failed: list[FailedRead]


@dataclass
class InputUnreadableDetails:
    """details[0] of an InputUnreadable error: the files none of whose candidates answered."""

    files: list[UnreadableFile]


@dataclass
class BadInputDetails:
    """details[0] of a BadInput error."""

    guid: str
    pfn: str
    reason: str


# --- af-io -------------------------------------------------------------------------------------


@dataclass
class CheckBuildRequest:
    tarball_path: str
    # "sha256:<hex>", as recorded on the production
    digest: str
    # Paths are refused unless they resolve (symlinks followed) under one of these
    allowed_roots: list[str]


@dataclass
class CheckBuildResult:
    readable: bool
    digest: str
    size: int


@dataclass
class VerifyRequest:
    unit_id: str
    attempt: int
    manifest_path: str
    output_path: str
    # Sum of Rucio event counts of the unit's input files (or max_events, for a smoke test)
    events_expected: int
    is_mc: bool
    allowed_roots: list[str]


@dataclass
class VerifyResult:
    """Result of verify_unit: what the output holds, read with uproot on a different worker."""

    unit_id: str
    attempt: int
    accepted: bool
    # Why not, when accepted is false: "missing output", "size mismatch", "checksum mismatch",
    # "missing tree", "missing CutBookkeeper", "events 900 != expected 1000", "sum of weights <= 0"
    reasons: list[str]
    size: int
    checksum: str
    events: int
    sum_of_weights: float
    cutflow: list[Cut]


@dataclass
class FetchLogRequest:
    path: str
    allowed_roots: list[str]
    # Bytes from the end of the file; capped at 64 KB by the activity
    tail_bytes: int = 65536


@dataclass
class LogExcerpt:
    path: str
    size: int
    text: str
    truncated: bool


@dataclass
class CleanupRequest:
    # Output, manifest and log paths of rejected or duplicate attempts
    paths: list[str]
    allowed_roots: list[str]


@dataclass
class CleanupResult:
    deleted: list[str]
    # Paths that were already gone; not an error, since activities retry
    missing: list[str]


@dataclass
class WriteListsRequest:
    production_id: str
    directory: str
    # List file name (e.g. "mc23a_Zee.txt") to the accepted output paths it lists
    lists: dict[str, list[str]]
    allowed_roots: list[str]


@dataclass
class WriteListsResult:
    paths: list[str]
