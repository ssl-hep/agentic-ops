"""The fixed-rule signature matcher, on log lines of the kinds EWRun, ROOT and xrootd print."""

import json
from pathlib import Path

from agentic_ops_worker.contracts import ErrorType
from agentic_ops_worker.signatures import MATCHED_MAX, Action, Evidence, Proposal, match_signatures

EXAMPLES = Path(__file__).resolve().parents[1] / "contract-examples"
LFN = "DAOD_PHYS.40123456._000017.pool.root.1"
GUID = "3F2A9C1E-1B7D-4C4E-9E2A-0D6B5A1C2E3F"


def evidence(log: str = "", error_type: str | None = ErrorType.CODE_ERROR, memclass: str = "4g", **kwargs) -> Evidence:
    return Evidence(
        error_type=error_type, message="EWRun.py exited with code 1", log_tail=log, memclass=memclass, **kwargs
    )


BAD_ALLOC_LOG = """\
EventLoop    INFO    Processing events 41000-42000
terminate called after throwing an instance of 'std::bad_alloc'
  what():  std::bad_alloc
"""


def test_bad_alloc_asks_for_the_next_memory_class() -> None:
    proposal = match_signatures(evidence(BAD_ALLOC_LOG, memclass="8g"))
    assert proposal is not None
    assert (proposal.action, proposal.signature, proposal.memclass) == (
        Action.RETRY_WITH_MEMORY,
        "std-bad-alloc",
        "16g",
    )
    assert "std::bad_alloc" in proposal.matched


def test_bad_alloc_on_the_largest_class_asks_for_a_split() -> None:
    proposal = match_signatures(evidence(BAD_ALLOC_LOG, memclass="16g"))
    assert proposal is not None and (proposal.action, proposal.parts) == (Action.SPLIT, 2)


def test_out_of_memory_error_type_without_a_log_line() -> None:
    proposal = match_signatures(evidence(error_type=ErrorType.OUT_OF_MEMORY))
    assert proposal == Proposal(
        Action.RETRY_WITH_MEMORY,
        "out-of-memory",
        "ran out of memory on 4g; retry on 8g",
        memclass="8g",
        matched="EWRun.py exited with code 1",
    )


def test_walltime_asks_for_a_split() -> None:
    proposal = match_signatures(evidence(error_type=ErrorType.WALLTIME, memclass="4g"))
    assert proposal is not None and (proposal.action, proposal.parts, proposal.signature) == (
        Action.SPLIT,
        2,
        "walltime",
    )


def test_truncated_file_named_in_the_log_is_excluded() -> None:
    log = (
        "Info in <TFile::Open>: opening file root://xcache.af.uchicago.edu:1094//root://fax.mwt2.org:1094/"
        f"/pnfs/uchicago.edu/atlasdatadisk/rucio/mc23_13p6TeV/4a/1c/{LFN}\n"
        f"Error in <TFile::Init>: file {LFN} is truncated at 2147483648 bytes: should be 4812331004, "
        "trying to recover\n"
    )
    proposal = match_signatures(
        evidence(log, lfn_to_guid={LFN: GUID, "DAOD_PHYS.40123456._000018.pool.root.1": "other"})
    )
    assert proposal is not None
    assert (proposal.action, proposal.signature, proposal.guids) == (Action.EXCLUDE_FILES, "corrupt-tfile", [GUID])


def test_corrupt_file_from_bad_input_details() -> None:
    details = json.loads((EXAMPLES / "bad_input_details.json").read_text())
    proposal = match_signatures(
        evidence(
            f"Error in <TBasket::Streamer>: The value of fNbytes is incorrect (-1) in {LFN}",
            error_type=ErrorType.BAD_INPUT,
            guids_failed=[details["guid"]],
        )
    )
    assert proposal is not None and proposal.guids == [details["guid"]]


def test_corrupt_file_that_cannot_be_named_is_escalated() -> None:
    proposal = match_signatures(evidence("R__unzip: error -5 in inflate (zlib)"))
    assert proposal is not None and (proposal.action, proposal.guids) == (Action.ESCALATE, [])


def test_memory_wins_over_a_corrupt_file_line() -> None:
    log = "Warning in <TFile::Init>: file x.root probably not closed, trying to recover\n" + BAD_ALLOC_LOG
    proposal = match_signatures(evidence(log))
    assert proposal is not None and proposal.action is Action.RETRY_WITH_MEMORY


def test_xrootd_3010_from_input_unreadable_details() -> None:
    details = json.loads((EXAMPLES / "input_unreadable_details.json").read_text())
    errors = [f["error"] for file in details["files"] for f in file["failed"]]
    proposal = match_signatures(evidence(error_type=ErrorType.INPUT_UNREADABLE, read_errors=errors))
    assert proposal is not None
    assert (proposal.action, proposal.signature, proposal.refresh_replicas) == (Action.RETRY, "xrootd-3010", True)
    assert proposal.matched.startswith("[3010]")


def test_nothing_recognised_goes_to_the_agent() -> None:
    log = (
        "Traceback (most recent call last):\n"
        '  File "EWRun.py", line 88, in <module>\n'
        "KeyError: 'r17610'\n"
        "processed 30100 events; [30100] items in cache; bad_alloc_count=0\n"
        "Info in <TFile::Open>: file opened fine, nothing truncated\n"
    )
    assert match_signatures(evidence(log)) is None


def test_matched_line_is_truncated() -> None:
    proposal = match_signatures(evidence("x" * 1000 + " std::bad_alloc"))
    assert proposal is not None and len(proposal.matched) == MATCHED_MAX
