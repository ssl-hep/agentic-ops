"""The certificate scheme, checked with cryptography's own path validation (WebPKI rules)."""

import stat
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.x509.verification import Criticality, ExtensionPolicy, PolicyBuilder, Store, VerificationError

from agentic_ops_pki import cli
from agentic_ops_pki.ca import (
    AF_IO_NAME,
    ROLE_CONTROLLER,
    ROLE_WORKER,
    PKIError,
    WorkerSigner,
    check_worker_name,
    load_certs,
    new_root,
    now,
    worker_name,
)


@pytest.fixture
def dev(tmp_path: Path) -> cli.Layout:
    assert cli.main(["--dir", str(tmp_path), "dev", "-n", "temporal.example.org", "-i", "192.0.2.7"]) == 0
    return cli.Layout(tmp_path)


def client_verifier(roots: list[x509.Certificate]):
    """WebPKI client rules, except that a SAN is optional: Go's TLS server does not need one."""
    ee = ExtensionPolicy.webpki_defaults_ee().may_be_present(x509.SubjectAlternativeName, Criticality.AGNOSTIC, None)
    policies = PolicyBuilder().extension_policies(ca_policy=ExtensionPolicy.webpki_defaults_ca(), ee_policy=ee)
    return policies.store(Store(roots)).build_client_verifier()


def client_chain(layout: cli.Layout, name: str) -> list[x509.Certificate]:
    """Verify a client directory the way the frontend does: leaf and intermediates against the root
    (ca.pem in the same directory, which is what the server's clientCaFiles holds)."""
    d = layout.client(name)
    leaf, *intermediates = load_certs(d / "cert.pem")
    return client_verifier(load_certs(d / "ca.pem")).verify(leaf, intermediates).chain


def ou(cert: x509.Certificate) -> str:
    return cert.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)[0].value


def cn(cert: x509.Certificate) -> str:
    return cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value


def test_root_signs_admin_controller_reader_directly(dev):
    for role in ("admin", "controller", "reader"):
        chain = client_chain(dev, role)
        assert len(chain) == 2 and ou(chain[0]) == role


def test_workers_come_from_the_worker_ca(dev):
    worker_ca = load_certs(dev.worker_ca / "ca.pem")[0]
    for name in ("ewkit-phase0", "ewkit-phase1", AF_IO_NAME):
        chain = client_chain(dev, name)
        assert len(chain) == 3 and chain[1] == worker_ca
        assert (ou(chain[0]), cn(chain[0])) == (ROLE_WORKER, name)


def test_negative_fixtures(dev):
    assert len(client_chain(dev, "root-signed-worker")) == 2
    assert ou(client_chain(dev, "worker-ca-controller")[0]) == ROLE_CONTROLLER
    with pytest.raises(VerificationError):
        client_chain(dev, "foreign")


def test_server_certificate(dev):
    server = dev.server
    leaf = load_certs(server / "server.pem")[0]
    root = load_certs(server / "ca.pem")
    for name in ("temporal.example.org", "localhost", "host.docker.internal"):
        PolicyBuilder().store(Store(root)).build_server_verifier(x509.DNSName(name)).verify(leaf, [])
    assert load_certs(server / "worker-ca.pem") == load_certs(dev.worker_ca / "ca.pem")
    assert {"192.0.2.7", "127.0.0.1"} <= set(cli.sans(leaf))


def test_worker_ca_cannot_mint_a_server_certificate(dev):
    """Clients trust the root, so a leaked worker CA must not be able to impersonate the server."""
    worker_ca = dev.worker_signer().authority
    fake = worker_ca.issue_server("temporal", ["temporal.example.org"], [], timedelta(days=1))
    verifier = (
        PolicyBuilder().store(Store([worker_ca.root])).build_server_verifier(x509.DNSName("temporal.example.org"))
    )
    with pytest.raises(VerificationError):
        verifier.verify(fake.cert, list(fake.intermediates))


def test_worker_ca_cannot_sign_another_ca(dev):
    worker_ca = dev.worker_signer().authority
    sub_ca = worker_ca.issue_ca("sub", "worker-ca", timedelta(days=1))
    leaf = sub_ca.issue_client(ROLE_WORKER, "ewkit-x", timedelta(hours=1))
    verifier = client_verifier([worker_ca.root])
    with pytest.raises(VerificationError):
        verifier.verify(leaf.cert, list(leaf.intermediates))


def test_private_files_are_private(dev):
    for path in [dev.root / "ca-key.pem", dev.worker_ca / "ca-key.pem", dev.client("ewkit-phase0") / "key.pem"]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert stat.S_IMODE(dev.client("ewkit-phase0").stat().st_mode) == 0o700


def test_signer_default_lifetime(dev):
    issued = WorkerSigner.from_dir(dev.worker_ca).sign_production("abc123")
    lifetime = issued.cert.not_valid_after_utc - now()
    assert timedelta(hours=29, minutes=59) < lifetime <= timedelta(hours=30)
    assert cn(issued.cert) == "ewkit-abc123"
    assert issued.cert_chain_pem.count(b"BEGIN CERTIFICATE") == 2


def test_worker_cannot_outlive_its_ca(dev):
    with pytest.raises(PKIError, match="rotate"):
        dev.worker_signer().sign("af-io", timedelta(days=400))


@pytest.mark.parametrize("bad", ["ewkit-", "ewkit-phase0-8g", "ewkit-Phase0", "someone", "af-io2", "ewkit-" + "a" * 41])
def test_worker_names_match_the_claim_mapper(bad):
    with pytest.raises(PKIError):
        check_worker_name(bad)


def test_good_worker_names():
    assert check_worker_name(AF_IO_NAME) == AF_IO_NAME
    assert worker_name("p2026a") == "ewkit-p2026a"
    with pytest.raises(PKIError):
        worker_name("with-dash")


def test_production_flow(tmp_path, monkeypatch, capsys):
    """root (encrypted) -> worker CA -> server -> clients -> workers -> rotate."""
    monkeypatch.setenv(cli.PASSPHRASE_ENV, "correct horse")
    run = lambda *a: cli.main(["--dir", str(tmp_path), *a])
    layout = cli.Layout(tmp_path)

    assert run("root") == 0
    assert b"ENCRYPTED" in (layout.root / "ca-key.pem").read_bytes()
    assert run("root") == 1, "the root is never replaced"
    assert run("worker-ca") == 0
    assert run("worker-ca") == 1, "replacing needs --rotate"
    assert run("server", "-n", "temporal.example.org") == 0
    assert run("client", "--role", "controller", "--name", "controller") == 0
    assert run("worker", "--production", "p1") == 0
    assert run("worker", "--af-io", "--out", str(tmp_path / "job")) == 0
    assert len(client_chain(layout, "controller")) == 2
    assert len(client_chain(layout, "ewkit-p1")) == 3

    assert run("test-clients", "--hours", "2") == 0
    test = cli.Layout(tmp_path / "test")
    assert len(client_chain(test, "reader")) == 2
    assert not (test.dir / "clients" / "admin").exists(), "no admin certificate for tests"
    assert (layout.client("controller") / "cert.pem").exists(), "the real clients are untouched"

    old_ca = load_certs(layout.worker_ca / "ca.pem")[0]
    assert run("worker-ca", "--rotate") == 0
    assert run("server", "-n", "temporal.example.org") == 0
    trusted = load_certs(layout.server / "worker-ca.pem")
    assert len(trusted) == 2 and old_ca in trusted
    assert len(client_chain(layout, "ewkit-p1")) == 3, "workers from the retired CA stay valid"

    monkeypatch.setenv(cli.PASSPHRASE_ENV, "wrong")
    assert run("client", "--role", "admin", "--name", "admin") == 1
    assert "wrong passphrase" in capsys.readouterr().err


def test_dev_keeps_its_cas(dev):
    root, worker_ca = (dev.root / "ca.pem").read_bytes(), (dev.worker_ca / "ca.pem").read_bytes()
    assert cli.main(["--dir", str(dev.dir), "dev"]) == 0
    assert (dev.root / "ca.pem").read_bytes() == root
    assert (dev.worker_ca / "ca.pem").read_bytes() == worker_ca


def test_show(dev, capsys):
    assert cli.main(["show", str(dev.client("ewkit-phase0"))]) == 0
    out = capsys.readouterr().out
    assert "OU=worker" in out and "OU=worker-ca" in out and "key.pem" not in out


def test_unrelated_root_is_not_a_worker_ca():
    with pytest.raises(PKIError):
        WorkerSigner(new_root("x", timedelta(days=1)))
