"""API tests against the fake GitLab in conftest.py."""

from fastapi.testclient import TestClient

from conftest import SHA, WEB


def test_status(client: TestClient) -> None:
    body = client.get("/api/ewkit/status").json()
    assert body["reachable"] is True
    assert body["token_configured"] is True
    assert body["default_branch"] == "master"


def test_refs(client: TestClient) -> None:
    body = client.get("/api/ewkit/refs").json()
    assert [b["name"] for b in body["branches"]] == ["master", "feature"]
    assert body["branches"][0]["default"] is True
    assert [t["name"] for t in body["tags"]] == ["v1.0"]


def test_analyses(client: TestClient) -> None:
    body = client.get("/api/ewkit/analyses").json()
    assert body["ref"]["sha"] == SHA
    assert body["image"] == {
        "tag": "master",
        "location": "registry.example/atlas/ewkit:master",
        "available": True,
        "digest": "sha256:abc",
        "created_at": "2026-08-01T01:00:00Z",
        "detail": None,
    }
    # Sorted case-insensitively; common is not an analysis
    by_folder = {a["folder"]: a for a in body["analyses"]}
    assert list(by_folder) == ["Broken", "RadiativeCSE", "RadiativeDecays", "SleptonDijetFF", "SUSYEWK1L2L"]

    radiative = by_folder["RadiativeDecays"]
    assert radiative["status"] == "runnable"
    assert radiative["code_owners"] == ["@bhodkins"]
    assert radiative["runs"] == ["Run2", "Run3"]
    assert radiative["campaigns"] == {"mc23a": 1, "mc23e": 1, "data15": 1, "unknown": 1}
    assert radiative["ntuple_lists"] == ["Run2_bkg.txt"]
    assert [s["name"] for s in radiative["sample_lists"]] == ["Run2_data", "Run3_bkg", "Run3_signal"]
    assert "Run3_signal.txt is empty" in radiative["warnings"]
    assert radiative["configs"][0]["output_tree"] == "analysis"

    assert by_folder["RadiativeCSE"]["status"] == "config_only"
    assert by_folder["RadiativeCSE"]["extends"] == "data/RadiativeDecays/config.yaml"
    assert by_folder["RadiativeCSE"]["code_owners"] == ["@default"]

    assert by_folder["SUSYEWK1L2L"]["status"] == "blocked"
    assert "TopCPToolkit" in by_folder["SUSYEWK1L2L"]["status_reason"]
    # Blocked through inheritance
    assert by_folder["SleptonDijetFF"]["status"] == "blocked"
    assert by_folder["Broken"]["status"] == "invalid"


def test_analysis_detail_and_sample_list(client: TestClient) -> None:
    detail = client.get("/api/ewkit/analyses/RadiativeDecays", params={"ref": "v1.0"}).json()
    assert detail["ref"]["ref"] == "v1.0"
    assert detail["image"]["available"] is False

    samples = client.get("/api/ewkit/analyses/RadiativeDecays/samples/Run3_bkg").json()
    assert samples["summary"]["datasets"] == 3
    assert samples["summary"]["problems"] == 1
    assert [g["heading"] for g in samples["groups"]] == ["Vjets: Z+jets", "odd"]
    assert samples["groups"][1]["datasets"][0]["problem"] == "Unknown r-tag r99999"


def test_config_file(client: TestClient) -> None:
    body = client.get("/api/ewkit/analyses/RadiativeCSE/configs/config.yaml").json()
    assert body["content"].startswith("Extend:")
    assert body["config"]["extend_chain"] == [
        {
            "path": "data/RadiativeDecays/config.yaml",
            "exists": True,
            "web_url": f"{WEB}/-/blob/{SHA}/data/RadiativeDecays/config.yaml",
        }
    ]


def test_bare_commit_has_no_image(client: TestClient) -> None:
    body = client.get("/api/ewkit/analyses", params={"ref": SHA[:8]}).json()
    assert body["image"]["tag"] is None


def test_not_found(client: TestClient) -> None:
    assert client.get("/api/ewkit/analyses", params={"ref": "nope"}).status_code == 404
    assert client.get("/api/ewkit/analyses/Nope").status_code == 404
    assert client.get("/api/ewkit/analyses/RadiativeDecays/samples/Run9_x").status_code == 404
