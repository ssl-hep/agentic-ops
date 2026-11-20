"""Unit planning: properties over random snapshots, and worked examples."""

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agentic_ops_worker.planning import PlanningParams, SnapshotFile, estimate_seconds, plan_units, unit_prefix
from agentic_ops_worker.units import S

DID = "mc23_13p6TeV:mc23_13p6TeV.601189.PhPy8EG_AZNLO_Zee.deriv.DAOD_PHYS.e8514_s4162_r15540_p6266"
GB = 10**9


@st.composite
def snapshots(draw: st.DrawFn) -> list[SnapshotFile]:
    n = draw(st.integers(0, 120))
    return [
        SnapshotFile(
            guid=f"g{i:04d}",
            lfn=draw(st.text("abcdef0123456789._", min_size=1, max_size=12)) + f".{i}",
            size=draw(st.integers(0, 20 * GB)),
            events=draw(st.integers(0, 200_000)),
            rse=draw(st.sampled_from([None, "MWT2_DATADISK", "INFN-T1_DATADISK", "SLOW_DATADISK"])),
        )
        for i in range(n)
    ]


param_sets = st.builds(
    PlanningParams,
    default_read_rate=st.sampled_from([5e6, 25e6, 250e6]),
    read_rate_by_rse=st.just({"MWT2_DATADISK": 900e6, "SLOW_DATADISK": 2e6}),
    events_per_second=st.sampled_from([50.0, 200.0, 1000.0]),
    target_seconds=st.sampled_from([600, 3 * 3600]),
    max_seconds=st.just(4 * 3600),
    max_files=st.sampled_from([1, 5, 200]),
)


@settings(max_examples=300, deadline=None)
@given(snapshots(), param_sets)
def test_planning_properties(files: list[SnapshotFile], params: PlanningParams) -> None:
    units = plan_units(DID, files, params)
    by_guid = {f.guid: f for f in files}
    # Every file in exactly one unit; no empty unit
    planned = [g for u in units for g in u.guids]
    assert sorted(planned) == sorted(by_guid)
    assert all(u.guids for u in units)
    # Files with a disk replica in PLANNED units, the others in BLOCKED units, in LFN order
    for state, on_disk in ((S.PLANNED, True), (S.BLOCKED, False)):
        guids = [g for u in units if u.state is state for g in u.guids]
        expected = sorted((f for f in files if (f.rse is not None) == on_disk), key=lambda f: (f.lfn, f.guid))
        assert guids == [f.guid for f in expected]
    # PLANNED units come first; IDs are unique, numbered in order and filename-safe
    assert [u.state for u in units] == sorted((u.state for u in units), key=lambda s: s is S.BLOCKED)
    assert [u.unit_id for u in units] == [f"{unit_prefix(DID)}-u{i:04d}" for i in range(len(units))]
    assert all(re.fullmatch(r"[A-Za-z0-9.-]+", u.unit_id) for u in units)
    for unit in units:
        assert len(unit.guids) <= params.max_files
        estimate = sum(estimate_seconds(by_guid[g], params) for g in unit.guids)
        if len(unit.guids) > 1:
            assert estimate <= params.max_seconds
        assert unit.est_seconds >= estimate
        assert unit.size == sum(by_guid[g].size for g in unit.guids)
        assert unit.events_expected == sum(by_guid[g].events for g in unit.guids)
        assert (unit.memclass, unit.attempt, unit.parent_id, unit.did) == (params.memclass, 0, None, DID)
    # Deterministic, whatever order the snapshot comes in
    assert plan_units(DID, list(reversed(files)), params) == units


def files_of(sizes_gb: list[float], rse: str | None = "MWT2_DATADISK") -> list[SnapshotFile]:
    return [SnapshotFile(f"g{i:03d}", f"DAOD.{i:06d}.root", int(s * GB), 0, rse) for i, s in enumerate(sizes_gb)]


def test_units_are_balanced() -> None:
    # 10 files of 100 s each at 1 GB/s; target 300 s gives ceil(1000/300) = 4 units of about 250 s
    params = PlanningParams(default_read_rate=GB, target_seconds=300, max_seconds=400)
    units = plan_units(DID, files_of([100] * 10), params)
    assert [len(u.guids) for u in units] == [3, 2, 3, 2]
    assert [u.est_seconds for u in units] == [300, 200, 300, 200]


def test_an_oversize_file_is_a_unit_of_its_own() -> None:
    params = PlanningParams(default_read_rate=GB, target_seconds=300, max_seconds=400)
    units = plan_units(DID, files_of([50, 1000, 50, 50]), params)
    assert [u.guids for u in units] == [("g000",), ("g001",), ("g002", "g003")]


def test_read_rate_per_rse() -> None:
    params = PlanningParams(
        default_read_rate=GB / 10, read_rate_by_rse={"MWT2_DATADISK": GB}, target_seconds=100, max_seconds=100
    )
    near = SnapshotFile("a", "a", 10 * GB, 2000, "MWT2_DATADISK")
    far = SnapshotFile("b", "b", 10 * GB, 2000, "TRIUMF-LCG2_DATADISK")
    tape = SnapshotFile("c", "c", 10 * GB, 2000, None)
    assert estimate_seconds(near, params) == 10 + 10
    assert estimate_seconds(far, params) == 100 + 10
    assert estimate_seconds(tape, params) == 100 + 10


def test_first_index_continues_numbering() -> None:
    units = plan_units(DID, files_of([1, 1]), PlanningParams(max_files=1), first_index=17)
    assert [u.unit_id for u in units] == ["601189-a2f173e1-u0017", "601189-a2f173e1-u0018"]


def test_tape_only_files_are_blocked() -> None:
    tape = SnapshotFile("t0", "DAOD.000000.root", GB, 0, None)
    units = plan_units(DID, [tape, *files_of([1, 1])], PlanningParams())
    assert [(u.state, u.guids) for u in units] == [(S.PLANNED, ("g000", "g001")), (S.BLOCKED, ("t0",))]


def test_empty_snapshot() -> None:
    assert plan_units(DID, [], PlanningParams()) == []


def test_duplicate_guids_refused() -> None:
    with pytest.raises(ValueError):
        plan_units(DID, files_of([1]) * 2, PlanningParams())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"default_read_rate": 0},
        {"events_per_second": -1},
        {"read_rate_by_rse": {"X": 0}},
        {"target_seconds": 0},
        {"target_seconds": 5, "max_seconds": 4},
        {"max_files": 0},
        {"memclass": "2g"},
    ],
)
def test_bad_params(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        PlanningParams(**kwargs)


@pytest.mark.parametrize(
    ("did", "prefix"),
    [
        (DID, "601189-a2f173e1"),
        ("data23_13p6TeV:data23_13p6TeV.00451234.physics_Main.deriv.DAOD_PHYS.r15243_p6266", "00451234-"),
        ("user.bengal1:user.bengal1.mytest.v1", ""),
    ],
)
def test_unit_prefix(did: str, prefix: str) -> None:
    assert unit_prefix(did).startswith(prefix)
    assert re.fullmatch(r"([0-9]+-)?[0-9a-f]{8}", unit_prefix(did))


def test_unit_prefix_tells_campaigns_of_one_dsid_apart() -> None:
    mc23a = "mc23_13p6TeV:mc23_13p6TeV.601189.PhPy8EG_AZNLO_Zee.deriv.DAOD_PHYS.e8514_s4162_r15540_p6266"
    mc23d = "mc23_13p6TeV:mc23_13p6TeV.601189.PhPy8EG_AZNLO_Zee.deriv.DAOD_PHYS.e8514_s4159_r15530_p6266"
    assert unit_prefix(mc23a) != unit_prefix(mc23d)
    assert unit_prefix(mc23a).split("-")[0] == unit_prefix(mc23d).split("-")[0] == "601189"


def test_unit_prefix_checks_the_did() -> None:
    with pytest.raises(ValueError):
        unit_prefix("not a did")
