"""Triage proposals, and the fixed rules that propose one without the agent (docs: Triage loop).

Pure: stdlib, contracts.py and units.py only, deterministic, no I/O. Runs in the match_signatures
activity on controller, so the rules can change without workflow versioning. Only failures no rule
recognises go to the agent.

Log text and error messages are untrusted: they are matched against fixed patterns and never
interpreted, and the line a rule matched is kept only as evidence, truncated.
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum

from agentic_ops_worker.contracts import ErrorType
from agentic_ops_worker.units import next_memclass


class Action(StrEnum):
    """The closed set of actions a proposal may make. The decide validator rejects any other."""

    RETRY = "retry"
    RETRY_WITH_MEMORY = "retry_with_memory"
    SPLIT = "split"
    EXCLUDE_FILES = "exclude_files"
    NEW_PRODUCTION = "new_production"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class Proposal:
    action: Action
    # The rule that made it ("std-bad-alloc", "xrootd-3010", ...), or "agent"
    signature: str
    rationale: str
    # retry_with_memory: the memory class to retry on
    memclass: str | None = None
    # split: how many parts
    parts: int | None = None
    # exclude_files: the files
    guids: list[str] = field(default_factory=list)
    # retry: run refresh_replicas first
    refresh_replicas: bool = False
    # The evidence line the rule matched, truncated
    matched: str = ""


@dataclass(frozen=True)
class Evidence:
    """What the fixed rules look at. collect_evidence (step 6) adds fields, with defaults."""

    error_type: str | None
    message: str
    log_tail: str
    memclass: str
    # GUIDs named by the error's details (BadInputDetails, InputUnreadableDetails)
    guids_failed: list[str] = field(default_factory=list)
    # The FailedRead errors from InputUnreadableDetails and the manifest
    read_errors: list[str] = field(default_factory=list)
    # LFN to GUID of the unit's files, to name a file a log line mentions
    lfn_to_guid: dict[str, str] = field(default_factory=dict)


MATCHED_MAX = 300

BAD_ALLOC = re.compile(r"std::bad_alloc")
XROOTD_3010 = re.compile(r"\[3010\]")
CORRUPT_TFILE = re.compile(
    r"Error in <TFile::Init>"
    r"|file \S+ is truncated at \d+ bytes"
    r"|probably not closed, trying to recover"
    r"|R__unzip: error"
    r"|error reading all requested bytes from file"
    r"|Error in <TBasket::Streamer>"
)


def _lines(evidence: Evidence) -> list[str]:
    return [*evidence.message.splitlines(), *evidence.read_errors, *evidence.log_tail.splitlines()]


def _first(pattern: re.Pattern[str], lines: list[str]) -> str | None:
    return next((line for line in lines if pattern.search(line)), None)


def _clip(line: str) -> str:
    line = line.strip()
    return line if len(line) <= MATCHED_MAX else line[: MATCHED_MAX - 1] + "…"


def _memory(evidence: Evidence, signature: str, matched: str) -> Proposal:
    bigger = next_memclass(evidence.memclass)
    if bigger:
        return Proposal(
            Action.RETRY_WITH_MEMORY,
            signature,
            f"ran out of memory on {evidence.memclass}; retry on {bigger}",
            memclass=bigger,
            matched=matched,
        )
    return Proposal(
        Action.SPLIT,
        signature,
        f"ran out of memory on {evidence.memclass}, the largest memory class; split the unit",
        parts=2,
        matched=matched,
    )


def match_signatures(evidence: Evidence) -> Proposal | None:
    """A proposal from the first rule that matches, or None for the agent.

    Rules, in order: running out of memory (more memory, or a split past the largest class); the
    watchdog's time limit (a split: more memory will not make it faster); a corrupt ROOT file
    (exclude it); xrootd [3010], a replica gone from its site (refresh replicas and retry).
    """
    lines = _lines(evidence)

    line = _first(BAD_ALLOC, lines)
    if line is not None:
        return _memory(evidence, "std-bad-alloc", _clip(line))
    if evidence.error_type == ErrorType.OUT_OF_MEMORY:
        return _memory(evidence, "out-of-memory", _clip(evidence.message))
    if evidence.error_type == ErrorType.WALLTIME:
        return Proposal(
            Action.SPLIT,
            "walltime",
            "the unit hit the time limit or stopped making progress; split it",
            parts=2,
            matched=_clip(evidence.message),
        )

    corrupt = [line for line in lines if CORRUPT_TFILE.search(line)]
    if corrupt:
        guids = list(evidence.guids_failed)
        if not guids:
            guids = sorted({guid for lfn, guid in evidence.lfn_to_guid.items() for ln in corrupt if lfn in ln})
        if guids:
            return Proposal(
                Action.EXCLUDE_FILES,
                "corrupt-tfile",
                "ROOT reports a corrupt input file; exclude it",
                guids=guids,
                matched=_clip(corrupt[0]),
            )
        return Proposal(
            Action.ESCALATE,
            "corrupt-tfile",
            "ROOT reports a corrupt input file, but the evidence does not say which",
            matched=_clip(corrupt[0]),
        )

    line = _first(XROOTD_3010, lines)
    if line is not None:
        return Proposal(
            Action.RETRY,
            "xrootd-3010",
            "a replica is no longer at its site; refresh replicas and retry",
            refresh_replicas=True,
            matched=_clip(line),
        )
    return None
