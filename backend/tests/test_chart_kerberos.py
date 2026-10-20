"""The Helm chart keeps the Kerberos password and ticket out of the backend container.

Renders the chart with `helm template` (skipped without helm; run `helm dependency build
charts/agentic-ops` first) and checks the backend pod: only the kerberos sidecar mounts the
kerberos-* volumes, and the backend has no service account token and no shared process namespace.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "charts" / "agentic-ops"
KERBEROS_VOLUMES = {"kerberos-password", "kerberos-config", "kerberos-ccache"}

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


def backend_pod(*values: str) -> dict:
    out = subprocess.run(
        ["helm", "template", "t", str(CHART), "-s", "templates/backend-deployment.yaml", *(f"--set={v}" for v in values)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    (deployment,) = [d for d in yaml.safe_load_all(out) if d]
    return deployment["spec"]["template"]["spec"]


def containers(pod: dict) -> dict[str, dict]:
    return {c["name"]: c for c in pod["containers"]}


def test_only_the_sidecar_mounts_the_kerberos_volumes() -> None:
    pod = backend_pod("kerberos.enabled=true", "kerberos.principal=someone@CERN.CH")
    assert pod["automountServiceAccountToken"] is False
    assert pod.get("shareProcessNamespace") is not True
    assert KERBEROS_VOLUMES <= {v["name"] for v in pod["volumes"]}

    by_name = containers(pod)
    assert set(by_name) == {"backend", "kerberos"}
    assert {m["name"] for m in by_name["kerberos"]["volumeMounts"]} == KERBEROS_VOLUMES
    assert not {m["name"] for m in by_name["backend"]["volumeMounts"]} & KERBEROS_VOLUMES

    env = {e["name"]: e.get("value") for e in by_name["backend"]["env"]}
    assert env["KRB5CCNAME"] == "FILE:/nonexistent/krb5cc"
    assert not [name for name in env if name.startswith("KRB_")]

    sidecar = by_name["kerberos"]["securityContext"]
    assert sidecar["runAsUser"] != 10001 and sidecar["readOnlyRootFilesystem"] and "ports" not in by_name["kerberos"]


def test_no_sidecar_or_kerberos_volumes_when_disabled() -> None:
    pod = backend_pod()
    assert set(containers(pod)) == {"backend"}
    assert not {v["name"] for v in pod["volumes"]} & KERBEROS_VOLUMES
    assert pod["automountServiceAccountToken"] is False
