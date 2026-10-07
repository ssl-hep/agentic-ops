"""Shared fixtures: a fake GitLab served through httpx.MockTransport, and an app client using it."""

import re
from collections.abc import Iterator
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import get_repository
from app.gitlab import ReadOnlyGitLabClient
from app.main import app
from app.repository import EWKitRepository

PROJECT = "atlas/EWKit"
SHA = "17c998c" + "0" * 33
WEB = "https://gitlab.example/atlas/EWKit"

MC23 = "mc23_13p6TeV.{dsid}.Sample.deriv.DAOD_PHYS.e8514_s4162_{rtag}_p7266"

FILES = {
    "CODEOWNERS": "* @default\n/data/RadiativeDecays/ @bhodkins\n",
    "data/common/config.yaml": "CommonServices: {}\n",
    "data/RadiativeDecays/config.yaml": "CommonServices: {}\nOutput:\n  treeName: 'analysis'\n",
    "data/RadiativeDecays/DAODs/Run3_bkg.txt": "# Vjets: Z+jets\n"
    + MC23.format(dsid="700786", rtag="r15540")
    + "\n"
    + MC23.format(dsid="700786", rtag="r16083")
    + "\n# odd\n"
    + MC23.format(dsid="700787", rtag="r99999")
    + "\n",
    "data/RadiativeDecays/DAODs/Run2_data.txt": "data15_13TeV.periodAllYear.physics_Main.PhysCont.DAOD_PHYS.grp15_v01_p7266\n",
    "data/RadiativeDecays/DAODs/Run3_signal.txt": "",
    "data/RadiativeDecays/ntuples/Run2_bkg.txt": "user.x.RadiativeDecays.1.a.mc20a.v1\n",
    "data/RadiativeCSE/config.yaml": "Extend: '../RadiativeDecays/config.yaml'\nTrigger: {}\n",
    "data/SleptonDijetFF/config.yaml": "Extend: '../SUSYEWK1L2L/config.yaml'\n",
    "data/SleptonDijetFF/DAODs/Run3_bkg.txt": MC23.format(dsid="700786", rtag="r15224") + "\n",
    "data/SUSYEWK1L2L/config.yaml": "CommonServices:\n  runSystematics: True\n"
    "AddConfigBlocks:\n  - modulePath: 'TopCPToolkit.BoostedJetTaggersConfig'\n",
    "data/SUSYEWK1L2L/DAODs/Run2_bkg.txt": MC23.format(dsid="700786", rtag="r15224") + "\n",
    "data/Broken/config.yaml": "Extend: '../Missing/config.yaml'\n",
    "data/Broken/DAODs/Run2_bkg.txt": "",
}


def _tree() -> list[dict[str, str]]:
    paths: dict[str, str] = {}
    for path in FILES:
        if not path.startswith("data/"):
            continue
        parts = path.split("/")
        for i in range(2, len(parts)):
            paths["/".join(parts[:i])] = "tree"
        paths[path] = "blob"
    return [{"path": p, "type": t, "name": p.rsplit("/", 1)[-1]} for p, t in sorted(paths.items())]


def fake_gitlab(request: httpx.Request) -> httpx.Response:
    assert request.method == "GET"
    assert request.headers.get("PRIVATE-TOKEN") == "test-token"
    path = request.url.raw_path.decode().split("?")[0]
    prefix = f"/api/v4/projects/{PROJECT.replace('/', '%2F')}"
    assert path.startswith(prefix), path
    rest = path[len(prefix) :]
    params = request.url.params

    if rest == "":
        return httpx.Response(200, json={"web_url": WEB, "default_branch": "master"})
    if rest == "/repository/branches":
        return httpx.Response(
            200,
            json=[
                {"name": "feature", "protected": False, "commit": {"id": "f" * 40, "committed_date": "2026-09-01T00:00:00Z"}},
                {"name": "master", "protected": True, "commit": {"id": SHA, "committed_date": "2026-08-01T00:00:00Z"}},
            ],
        )
    if rest == "/repository/tags":
        return httpx.Response(200, json=[{"name": "v1.0", "protected": True, "commit": {"id": SHA}}])
    if m := re.fullmatch(r"/repository/commits/(.+)", rest):
        if unquote(m[1]) not in ("master", "v1.0", SHA, SHA[:8]):
            return httpx.Response(404, json={"message": "404 Commit Not Found"})
        return httpx.Response(
            200, json={"id": SHA, "short_id": SHA[:8], "title": "Merge branch", "web_url": f"{WEB}/-/commit/{SHA}"}
        )
    if rest == "/repository/tree":
        assert params["ref"] == SHA and params["path"] == "data"
        return httpx.Response(200, json=_tree())
    if m := re.fullmatch(r"/repository/files/(.+)/raw", rest):
        assert params["ref"] == SHA
        file_path = unquote(m[1])
        if file_path not in FILES:
            return httpx.Response(404, json={"message": "404 File Not Found"})
        return httpx.Response(200, text=FILES[file_path])
    if rest == "/registry/repositories":
        return httpx.Response(200, json=[{"id": 7, "path": "atlas/ewkit", "location": "registry.example/atlas/ewkit"}])
    if m := re.fullmatch(r"/registry/repositories/7/tags/(.+)", rest):
        if m[1] != "master":
            return httpx.Response(404, json={"message": "404 Tag Not Found"})
        return httpx.Response(200, json={"digest": "sha256:abc", "created_at": "2026-08-01T01:00:00Z"})
    return httpx.Response(500, json={"message": f"unexpected {rest}"})


def make_repository() -> EWKitRepository:
    gitlab = ReadOnlyGitLabClient(
        "https://gitlab.example", PROJECT, "test-token", transport=httpx.MockTransport(fake_gitlab)
    )
    return EWKitRepository(gitlab)


@pytest.fixture
def repository() -> EWKitRepository:
    return make_repository()


@pytest.fixture
def client(repository: EWKitRepository) -> Iterator[TestClient]:
    app.dependency_overrides[get_repository] = lambda: repository
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
