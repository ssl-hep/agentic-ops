"""Validator checks against the fake GitLab in conftest.py."""

import asyncio

from app.repository import EWKitRepository
from app.runspec import RunSpec
from app.validator import validate


def run(repo: EWKitRepository, **fields) -> dict[str, list]:
    spec = RunSpec(**{"analysis": "RadiativeDecays", "ref": "v1.0", "runs": ["Run3"], "sample_lists": ["bkg"], **fields})

    async def go():
        return await validate(repo, await repo.resolve(spec.ref), spec)

    report = asyncio.run(go())
    by_code: dict[str, list] = {}
    for f in report.findings:
        by_code.setdefault(f.code, []).append(f)
    by_code["_selection"] = report.selection
    by_code["_ref_kind"] = [report.ref_kind]
    return by_code


def test_unknown_rtag_and_skip_patterns(repository: EWKitRepository) -> None:
    found = run(repository)
    assert found["_ref_kind"] == ["tag"]
    [unknown] = found["UNKNOWN_RTAG"]
    assert unknown.severity == "blocking"
    assert "1 of 3" in unknown.message and "r99999" in unknown.message
    assert unknown.evidence[0].path == "data/RadiativeDecays/DAODs/Run3_bkg.txt"
    assert unknown.evidence[0].line_numbers == [5]
    assert unknown.evidence[0].web_url.endswith("Run3_bkg.txt#L5")
    assert found["_selection"][0].selected == 3
    assert unknown.suggestion == {"skip": ["_r99999_"]}
    assert run(repository, skip=["x"])["UNKNOWN_RTAG"][0].suggestion == {"skip": ["x", "_r99999_"]}
    assert "NO_IMAGE_FOR_REF" in found  # CI only pushed an image for master
    assert "REF_POLICY" not in found

    skipped = run(repository, skip=["_r99999_"])
    assert "UNKNOWN_RTAG" not in skipped
    assert skipped["_selection"][0].selected == 2
    assert skipped["_selection"][0].campaigns == {"mc23a": 1, "mc23e": 1}

    assert "UNKNOWN_RTAG" not in run(repository, include=["700786"])


def test_ref_policy(repository: EWKitRepository) -> None:
    [policy] = run(repository, ref="master")["REF_POLICY"]
    assert policy.severity == "blocking"
    assert policy.suggestion == {"purpose": "test"}
    assert "REF_POLICY" not in run(repository, ref="master", purpose="test")


def test_sample_list_names(repository: EWKitRepository) -> None:
    found = run(repository, sample_lists=["sig"])
    assert "Did you mean 'signal'" in found["SAMPLE_LIST_NOT_FOUND"][0].message
    assert found["SAMPLE_LIST_NOT_FOUND"][0].suggestion == {"sample_lists": ["signal"]}

    found = run(repository, sample_lists=["signal"])
    assert found["EMPTY_LIST"][0].severity == "warning"
    assert "EMPTY_SELECTION" in found

    found = run(repository, runs=["Run2", "Run3"])
    assert found["SAMPLE_LIST_MISSING_FOR_RUN"][0].id == "SAMPLE_LIST_MISSING_FOR_RUN:Run2_bkg"

    found = run(repository, runs=["Run4"], sample_lists=[])
    assert {"RUN_NOT_AVAILABLE", "NO_SAMPLE_LISTS"} <= found.keys()


def test_folder_and_config_checks(repository: EWKitRepository) -> None:
    found = run(repository, analysis="SUSYEWK1L2L", runs=["Run2"])
    assert "CONFIG_NEEDS_UNAVAILABLE_MODULES" in found
    assert "NOT_IN_CI" in found
    assert "SYSTEMATICS_DEFAULT_ON" in found

    # Inherited through Extend
    found = run(repository, analysis="SleptonDijetFF")
    assert {"CONFIG_NEEDS_UNAVAILABLE_MODULES", "EXTEND_READONLY_IMAGE", "SYSTEMATICS_DEFAULT_ON"} <= found.keys()
    assert "SYSTEMATICS_DEFAULT_ON" not in run(repository, analysis="SleptonDijetFF", systematics="do")

    assert "EXTEND_MISSING" in run(repository, analysis="Broken", runs=["Run2"])
    assert "FOLDER_NOT_RUNNABLE" in run(repository, analysis="RadiativeCSE", runs=[])
    assert "CONFIG_NOT_FOUND" in run(repository, config="config_sys.yaml")
    assert "Did you mean 'RadiativeDecays'" in run(repository, analysis="RadiativDecays")["ANALYSIS_NOT_FOUND"][0].message


def test_invalid_pattern_and_memory(repository: EWKitRepository) -> None:
    found = run(repository, skip=["(unclosed"], memory_mb=12000)
    assert found["INVALID_PATTERN"][0].field == "skip"
    assert found["MEMORY_OUT_OF_RANGE"][0].severity == "warning"
