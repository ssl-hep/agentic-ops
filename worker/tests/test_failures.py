"""Failure classification and the dataset workflow's next step after each failure."""

import pytest

from agentic_ops_worker.contracts import ErrorType
from agentic_ops_worker.failures import (
    Block,
    FailureClass,
    FailureHistory,
    FailurePolicy,
    Retry,
    Triage,
    classify,
    decide_next,
    record,
)

F = FailureClass
POLICY = FailurePolicy()


@pytest.mark.parametrize(
    ("error_type", "timed_out", "cls"),
    [
        (None, True, F.INFRASTRUCTURE),
        (None, False, F.INFRASTRUCTURE),
        (ErrorType.CODE_ERROR, True, F.INFRASTRUCTURE),
        (ErrorType.INPUT_UNREADABLE, False, F.TRANSIENT_INPUT),
        (ErrorType.OUT_OF_MEMORY, False, F.RESOURCES),
        (ErrorType.WALLTIME, False, F.RESOURCES),
        (ErrorType.BAD_INPUT, False, F.BAD_INPUT),
        (ErrorType.CODE_ERROR, False, F.CODE),
        ("SomethingNew", False, F.CODE),
    ],
)
def test_classify(error_type: str | None, timed_out: bool, cls: FailureClass) -> None:
    assert classify(error_type, timed_out=timed_out) is cls


def walk(cls: FailureClass, n: int, **kwargs: str) -> list:
    history = FailureHistory()
    steps = []
    for _ in range(n):
        history = record(history, cls, **kwargs)
        steps.append(decide_next(cls, history, POLICY, guid=kwargs.get("guid")))
    return steps


def test_infrastructure_backs_off_then_triages() -> None:
    assert walk(F.INFRASTRUCTURE, 4) == [Retry(60), Retry(120), Retry(240), Triage("4 infrastructure failures")]


def test_transient_input_refreshes_then_blocks() -> None:
    assert walk(F.TRANSIENT_INPUT, 4) == [
        Retry(60, refresh_replicas=True),
        Retry(120, refresh_replicas=True),
        Retry(240, refresh_replicas=True),
        Block("inputs unreadable after 4 attempts"),
    ]


@pytest.mark.parametrize("cls", [F.RESOURCES, F.CODE])
def test_resources_and_code_go_straight_to_triage(cls: FailureClass) -> None:
    (step,) = walk(cls, 1)
    assert isinstance(step, Triage)


def test_bad_input_on_a_second_host_is_triaged() -> None:
    history = record(FailureHistory(), F.BAD_INPUT, guid="g1", host="node-a")
    assert decide_next(F.BAD_INPUT, history, POLICY, guid="g1") == Retry(60)
    history = record(history, F.BAD_INPUT, guid="g1", host="node-b")
    assert decide_next(F.BAD_INPUT, history, POLICY, guid="g1") == Triage("file g1 failed on 2 hosts", guid="g1")
    assert history.bad_input_hosts == {"g1": ["node-a", "node-b"]}


def test_bad_input_on_one_host_is_triaged_after_the_retries() -> None:
    steps = walk(F.BAD_INPUT, 3, guid="g1", host="node-a")
    assert steps == [Retry(60), Retry(120), Triage("3 bad input failures", guid="g1")]


def test_rejected_output_retries_once() -> None:
    assert walk(F.REJECTED, 2) == [Retry(60), Triage("output rejected by verification 2 times")]


def test_backoff_is_capped() -> None:
    policy = FailurePolicy(max_infrastructure=20, backoff_base_seconds=60, backoff_max_seconds=600)
    assert [policy.backoff(n) for n in range(1, 7)] == [60, 120, 240, 480, 600, 600]


def test_history_is_not_mutated() -> None:
    before = FailureHistory()
    after = record(before, F.BAD_INPUT, guid="g1", host="h")
    assert (before.counts, before.bad_input_hosts) == ({}, {})
    assert after.count(F.BAD_INPUT) == 1 and after.count(F.CODE) == 0


def test_every_class_has_a_decision() -> None:
    for cls in FailureClass:
        assert isinstance(decide_next(cls, record(FailureHistory(), cls), POLICY), Retry | Block | Triage)
