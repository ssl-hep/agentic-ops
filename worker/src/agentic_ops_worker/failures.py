"""Failure classes of run_unit and what the dataset workflow does next (docs: Retry policy).

Pure: stdlib and contracts.py only, deterministic, no I/O. The dataset workflow calls it on every
failed attempt, since Temporal never retries run_unit (maximum_attempts=1): each retry is a new
attempt with its own output path.
"""

from dataclasses import dataclass, field, replace
from enum import StrEnum

from agentic_ops_worker.contracts import ErrorType


class FailureClass(StrEnum):
    # Eviction, heartbeat timeout, lost worker or node: the activity timed out, no error type
    INFRASTRUCTURE = "infrastructure"
    # InputUnreadable: no candidate replica of a file answered
    TRANSIENT_INPUT = "transient_input"
    # OutOfMemory, Walltime
    RESOURCES = "resources"
    # BadInput: a file is bad in a way a retry on the same file may not fix
    BAD_INPUT = "bad_input"
    # CodeError, or an error type this code does not know
    CODE = "code"
    # run_unit succeeded but verify_unit rejected the output. Not an error type: the workflow
    # assigns it from the VerifyResult
    REJECTED = "rejected"


_BY_TYPE = {
    ErrorType.INPUT_UNREADABLE: FailureClass.TRANSIENT_INPUT,
    ErrorType.OUT_OF_MEMORY: FailureClass.RESOURCES,
    ErrorType.WALLTIME: FailureClass.RESOURCES,
    ErrorType.BAD_INPUT: FailureClass.BAD_INPUT,
    ErrorType.CODE_ERROR: FailureClass.CODE,
}


def classify(error_type: str | None, *, timed_out: bool) -> FailureClass:
    """The class of a failed run_unit attempt: its ApplicationError type, or a timeout."""
    if timed_out or error_type is None:
        return FailureClass.INFRASTRUCTURE
    return _BY_TYPE.get(error_type, FailureClass.CODE)


@dataclass(frozen=True)
class FailurePolicy:
    # Retries allowed per class before the unit is triaged (or BLOCKED, for transient input)
    max_infrastructure: int = 3
    max_transient_input: int = 3
    max_bad_input: int = 2
    max_rejected: int = 1
    # Backoff before retry n is base * 2**(n-1), capped
    backoff_base_seconds: int = 60
    backoff_max_seconds: int = 3600

    def backoff(self, n: int) -> int:
        return min(self.backoff_base_seconds * 2 ** max(0, n - 1), self.backoff_max_seconds)


@dataclass(frozen=True)
class FailureHistory:
    """A unit's failures so far, kept in workflow state: small and JSON-shaped."""

    # Failures per FailureClass value
    counts: dict[str, int] = field(default_factory=dict)
    # BadInput failures: GUID to the hosts it failed on, in order
    bad_input_hosts: dict[str, list[str]] = field(default_factory=dict)

    def count(self, cls: FailureClass) -> int:
        return self.counts.get(cls.value, 0)


def record(
    history: FailureHistory, cls: FailureClass, guid: str | None = None, host: str | None = None
) -> FailureHistory:
    """The history with one more failure; guid and host are BadInputDetails.guid and the manifest's
    or worker's host."""
    counts = {**history.counts, cls.value: history.count(cls) + 1}
    hosts = history.bad_input_hosts
    if cls is FailureClass.BAD_INPUT and guid is not None:
        hosts = {**hosts, guid: [*hosts.get(guid, []), host or "unknown"]}
    return replace(history, counts=counts, bad_input_hosts=hosts)


@dataclass(frozen=True)
class Retry:
    """Start a new attempt after backoff_seconds; run refresh_replicas first if asked."""

    backoff_seconds: int
    refresh_replicas: bool = False


@dataclass(frozen=True)
class Block:
    """The unit is BLOCKED and triaged: its inputs could not be read after every retry."""

    reason: str


@dataclass(frozen=True)
class Triage:
    """The unit goes to triage (collect_evidence, fixed rules, then the agent)."""

    reason: str
    # The file to propose excluding, for bad input
    guid: str | None = None


Next = Retry | Block | Triage


def decide_next(cls: FailureClass, history: FailureHistory, policy: FailurePolicy, guid: str | None = None) -> Next:
    """What to do after a failure of class cls; history already includes it."""
    n = history.count(cls)
    match cls:
        case FailureClass.INFRASTRUCTURE:
            if n <= policy.max_infrastructure:
                return Retry(policy.backoff(n))
            return Triage(f"{n} infrastructure failures")
        case FailureClass.TRANSIENT_INPUT:
            if n <= policy.max_transient_input:
                return Retry(policy.backoff(n), refresh_replicas=True)
            return Block(f"inputs unreadable after {n} attempts")
        case FailureClass.RESOURCES:
            return Triage("out of memory or walltime")
        case FailureClass.CODE:
            return Triage("code or configuration error")
        case FailureClass.BAD_INPUT:
            hosts = set(history.bad_input_hosts.get(guid, [])) if guid else set()
            if len(hosts) >= 2:
                return Triage(f"file {guid} failed on {len(hosts)} hosts", guid=guid)
            if n > policy.max_bad_input:
                return Triage(f"{n} bad input failures", guid=guid)
            return Retry(policy.backoff(n))
        case FailureClass.REJECTED:
            if n <= policy.max_rejected:
                return Retry(policy.backoff(n))
            return Triage(f"output rejected by verification {n} times")
