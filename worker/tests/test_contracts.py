"""The example JSON files are exactly what the contract dataclasses encode to and decode from.

Decoding then re-encoding must give the file back unchanged: an unknown key in a file would be
dropped (the converter ignores it) and a missing key with a default would be added, so either
shows up as a difference.
"""

import dataclasses
import json
from pathlib import Path

import pytest
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter

from agentic_ops_worker import contracts as c

EXAMPLES = Path(__file__).resolve().parents[1] / "contract-examples"

PAYLOADS = {
    "unit_spec": c.UnitSpec,
    "unit_heartbeat": c.UnitHeartbeat,
    "manifest": c.Manifest,
    "input_unreadable_details": c.InputUnreadableDetails,
    "bad_input_details": c.BadInputDetails,
    "check_build_request": c.CheckBuildRequest,
    "check_build_result": c.CheckBuildResult,
    "verify_request": c.VerifyRequest,
    "verify_result": c.VerifyResult,
    "fetch_log_request": c.FetchLogRequest,
    "log_excerpt": c.LogExcerpt,
    "cleanup_request": c.CleanupRequest,
    "cleanup_result": c.CleanupResult,
    "write_lists_request": c.WriteListsRequest,
    "write_lists_result": c.WriteListsResult,
}

converter = DataConverter.default.payload_converter


def nested_dataclasses(cls: type) -> set[type]:
    """cls and every dataclass its fields refer to, directly or in a list or dict."""
    found = {cls}
    for f in dataclasses.fields(cls):
        for t in [f.type, *getattr(f.type, "__args__", ())]:
            if dataclasses.is_dataclass(t) and t not in found:
                found |= nested_dataclasses(t)
    return found


def test_every_example_has_a_payload_and_back() -> None:
    assert {p.stem for p in EXAMPLES.glob("*.json")} == set(PAYLOADS)


def test_every_contract_dataclass_is_covered() -> None:
    defined = {v for v in vars(c).values() if dataclasses.is_dataclass(v) and v.__module__ == c.__name__}
    covered = set().union(*(nested_dataclasses(cls) for cls in PAYLOADS.values()))
    assert defined == covered


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_example_round_trips(name: str) -> None:
    raw = (EXAMPLES / f"{name}.json").read_bytes()
    payload = Payload(metadata={"encoding": b"json/plain"}, data=raw)
    value = converter.from_payload(payload, PAYLOADS[name])
    assert isinstance(value, PAYLOADS[name])
    assert json.loads(converter.to_payload(value).data) == json.loads(raw)


def test_error_types_are_distinct() -> None:
    types = [v for k, v in vars(c.ErrorType).items() if k.isupper() and isinstance(v, str)]
    assert len(types) == len(set(types))
    assert set(c.ErrorType.NON_RETRYABLE) <= set(types)
    assert c.ErrorType.INPUT_UNREADABLE not in c.ErrorType.NON_RETRYABLE
