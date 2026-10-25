"""agentic-ops-pki: make and use the certificates for the agentic-ops Temporal server.

    agentic-ops-pki --dir DIR root                         # once: the offline root CA
    agentic-ops-pki --dir DIR worker-ca [--rotate]         # the worker-only intermediate
    agentic-ops-pki --dir DIR server -n NAME [-i IP]...    # frontend certificate
    agentic-ops-pki --dir DIR client --role ROLE --name CN # admin, controller, reader
    agentic-ops-pki --dir DIR worker --production ID       # what the ops-executor does per job
    agentic-ops-pki --dir DIR test-clients                 # clients for test_authz.py
    agentic-ops-pki --dir DIR dev [-n NAME] [-i IP]        # everything for local tests
    agentic-ops-pki show PATH...

DIR (default secrets/temporal-pki in this repository) is laid out as:

    root/ca.pem, root/ca-key.pem          the root; the key is encrypted, keep it offline
    worker-ca/ca.pem, ca-key.pem, root.pem  the worker CA: the ops-executor's Secret
    worker-ca/retired/<serial>.pem        worker CAs replaced by --rotate
    server/server.pem, server-key.pem, ca.pem, worker-ca.pem   the server's Secret
    clients/<name>/cert.pem, key.pem, ca.pem                   one directory per client

The root key's passphrase is read from AGENTIC_OPS_PKI_PASSPHRASE, or prompted for.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import ExtensionOID

from agentic_ops_pki.ca import (
    AF_IO_NAME,
    ROLE_ADMIN,
    ROLE_CONTROLLER,
    ROLE_READER,
    ROLE_WORKER,
    ROOT_SIGNED_ROLES,
    WORKER_LIFETIME,
    Authority,
    Issued,
    PKIError,
    WorkerSigner,
    key_is_encrypted,
    key_pem,
    load_authority,
    load_certs,
    new_root,
    now,
    pem,
    worker_name,
    write_file,
    write_private_dir,
)

PASSPHRASE_ENV = "AGENTIC_OPS_PKI_PASSPHRASE"
DEFAULT_DIR = Path(__file__).resolve().parents[3] / "secrets" / "temporal-pki"

# Default lifetimes. Admin is short: it is used rarely and re-issued from the root when needed.
ROOT_DAYS = 3650
WORKER_CA_DAYS = 365
SERVER_DAYS = 365
CLIENT_DAYS = {ROLE_ADMIN: 30, ROLE_CONTROLLER: 365, ROLE_READER: 365}

# Always in the server certificate, for kubectl port-forward and the local compose server
LOCAL_NAMES = ["localhost"]
LOCAL_IPS = ["127.0.0.1"]


@dataclass(frozen=True)
class Layout:
    dir: Path

    @property
    def root(self) -> Path:
        return self.dir / "root"

    @property
    def worker_ca(self) -> Path:
        return self.dir / "worker-ca"

    @property
    def server(self) -> Path:
        return self.dir / "server"

    def client(self, name: str) -> Path:
        return self.dir / "clients" / name

    def load_root(self) -> Authority:
        cert, key = self.root / "ca.pem", self.root / "ca-key.pem"
        if not key.exists():
            raise PKIError(f"no root CA key at {key} (run `root` first, or bring the offline key back)")
        passphrase = read_passphrase(confirm=False) if key_is_encrypted(key) else None
        try:
            return load_authority(cert, key, passphrase=passphrase)
        except ValueError as e:
            raise PKIError(f"cannot read {key}: wrong passphrase?") from e

    def worker_signer(self) -> WorkerSigner:
        if not (self.worker_ca / "ca-key.pem").exists():
            raise PKIError(f"no worker CA in {self.worker_ca} (run `worker-ca` first)")
        return WorkerSigner.from_dir(self.worker_ca)

    def worker_cas(self) -> list[x509.Certificate]:
        """The current worker CA and the retired ones that have not expired: what the server trusts."""
        certs = load_certs(self.worker_ca / "ca.pem")
        retired = self.worker_ca / "retired"
        if retired.is_dir():
            for f in sorted(retired.glob("*.pem")):
                certs += [c for c in load_certs(f) if c.not_valid_after_utc > now()]
        return certs


def read_passphrase(*, confirm: bool) -> bytes:
    if value := os.environ.get(PASSPHRASE_ENV):
        return value.encode()
    first = getpass.getpass("Root CA key passphrase: ")
    if confirm and getpass.getpass("Again: ") != first:
        raise PKIError("passphrases differ")
    if not first:
        raise PKIError("empty passphrase")
    return first.encode()


# Commands


def cmd_root(layout: Layout, args: argparse.Namespace) -> None:
    if (layout.root / "ca.pem").exists():
        raise PKIError(f"a root CA already exists in {layout.root}; it is never replaced by this tool")
    passphrase = None if args.no_passphrase else read_passphrase(confirm=True)
    root = new_root(args.common_name, timedelta(days=args.days))
    write_root(layout, root, passphrase)
    print(f"Root CA in {layout.root}. Keep ca-key.pem offline; every other command reads only ca.pem from here.")


def write_root(layout: Layout, root: Authority, passphrase: bytes | None) -> None:
    write_private_dir(layout.root)
    write_file(layout.root / "ca.pem", pem(root.cert), 0o644)
    write_file(layout.root / "ca-key.pem", key_pem(root.key, passphrase))


def cmd_worker_ca(layout: Layout, args: argparse.Namespace) -> None:
    current = layout.worker_ca / "ca.pem"
    if current.exists() and not args.rotate:
        raise PKIError(f"a worker CA already exists in {layout.worker_ca}; pass --rotate to replace it")
    root = layout.load_root()
    if current.exists():
        retire_worker_ca(layout)
    worker_ca = root.issue_ca(args.common_name, "worker-ca", timedelta(days=args.days))
    write_worker_ca(layout, worker_ca)
    print(f"Worker CA in {layout.worker_ca}, valid until {worker_ca.not_after:%Y-%m-%d}.")
    print("Re-issue the server certificate (`server`) so the server trusts it, and update the ops-executor Secret.")


def retire_worker_ca(layout: Layout) -> None:
    """Keep the old certificate, so the server accepts its workers until they expire; drop its key."""
    old = x509.load_pem_x509_certificate((layout.worker_ca / "ca.pem").read_bytes())
    retired = layout.worker_ca / "retired"
    write_private_dir(retired)
    write_file(retired / f"{old.serial_number:x}.pem", pem(old), 0o644)
    (layout.worker_ca / "ca-key.pem").unlink()


def write_worker_ca(layout: Layout, worker_ca: Authority) -> None:
    write_private_dir(layout.worker_ca)
    write_file(layout.worker_ca / "ca.pem", pem(worker_ca.cert), 0o644)
    write_file(layout.worker_ca / "ca-key.pem", key_pem(worker_ca.key))
    write_file(layout.worker_ca / "root.pem", pem(worker_ca.root), 0o644)


def cmd_server(layout: Layout, args: argparse.Namespace) -> None:
    root = layout.load_root()
    issued = issue_server(layout, root, args.name, args.ip, args.days, args.common_name)
    print(f"Server certificate in {layout.server}, SANs: {', '.join(sans(issued.cert))}")


def issue_server(
    layout: Layout, root: Authority, names: list[str], ips: list[str], days: int, common_name: str
) -> Issued:
    worker_cas = layout.worker_cas()
    issued = root.issue_server(common_name, unique(LOCAL_NAMES + names), unique(LOCAL_IPS + ips), timedelta(days=days))
    # The server runs as a non-root user and reads these from a mounted directory (compose
    # bind mount, or a Secret volume), so they are readable by all, unlike the client files.
    layout.server.mkdir(parents=True, exist_ok=True)
    layout.server.chmod(0o755)
    write_file(layout.server / "server.pem", issued.cert_chain_pem, 0o644)
    write_file(layout.server / "server-key.pem", issued.key_pem, 0o644)
    write_file(layout.server / "ca.pem", issued.ca_pem, 0o644)
    write_file(layout.server / "worker-ca.pem", b"".join(pem(c) for c in worker_cas), 0o644)
    return issued


def cmd_client(layout: Layout, args: argparse.Namespace) -> None:
    root = layout.load_root()
    days = args.days or CLIENT_DAYS[args.role]
    out = root.issue_client(args.role, args.name, timedelta(days=days)).write_client_dir(
        args.out or layout.client(args.name)
    )
    print(f"{args.role} certificate CN={args.name} in {out}, valid {days} days")


def cmd_worker(layout: Layout, args: argparse.Namespace) -> None:
    signer = layout.worker_signer()
    name = AF_IO_NAME if args.af_io else worker_name(args.production)
    issued = signer.sign(name, timedelta(hours=args.hours))
    out = issued.write_client_dir(args.out or layout.client(name))
    print(f"worker certificate CN={name} in {out}, valid until {issued.cert.not_valid_after_utc:%Y-%m-%d %H:%M} UTC")


def cmd_dev(layout: Layout, args: argparse.Namespace) -> None:
    """An unencrypted CA, the server certificate and every client the local tests need.

    Keeps an existing root and worker CA, so re-running (e.g. to add -i) keeps clients valid.
    """
    if (layout.root / "ca-key.pem").exists():
        root = layout.load_root()
    else:
        root = new_root("agentic-ops dev root CA", timedelta(days=ROOT_DAYS))
        write_root(layout, root, None)
    if (layout.worker_ca / "ca-key.pem").exists():
        worker_ca = layout.worker_signer().authority
    else:
        worker_ca = root.issue_ca("agentic-ops dev worker CA", "worker-ca", timedelta(days=WORKER_CA_DAYS))
        write_worker_ca(layout, worker_ca)
    issued = issue_server(layout, root, ["host.docker.internal", *args.name], args.ip, 90, "temporal-dev")
    issue_test_clients(root, worker_ca, layout.dir, timedelta(hours=args.hours))
    root.issue_client(ROLE_ADMIN, "admin", timedelta(days=30)).write_client_dir(layout.client("admin"))
    print(f"Development certificates in {layout.dir} (server SANs: {', '.join(sans(issued.cert))})")


def cmd_test_clients(layout: Layout, args: argparse.Namespace) -> None:
    out = args.out or layout.dir / "test"
    issue_test_clients(layout.load_root(), layout.worker_signer().authority, out, timedelta(hours=args.hours))
    print(
        f"Test clients in {out}/clients, valid {args.hours:g} hours: PHASE0_CERTS={out} for phase0/tests/test_authz.py"
    )


def issue_test_clients(root: Authority, worker_ca: Authority, out: Path, lifetime: timedelta) -> None:
    """The clients phase0/tests/test_authz.py uses, in <out>/clients (replaced)."""
    clients = out / "clients"
    if clients.exists():
        shutil.rmtree(clients)
    for role in (ROLE_CONTROLLER, ROLE_READER):
        root.issue_client(role, role, lifetime).write_client_dir(clients / role)
    signer = WorkerSigner(worker_ca)
    for name in ("ewkit-phase0", "ewkit-phase1", AF_IO_NAME):
        signer.sign(name, lifetime).write_client_dir(clients / name)

    # Must be refused: a client from an unrelated CA (at the handshake), a worker certificate
    # the root signed, and a controller the worker CA signed (by the claim mapper)
    foreign = new_root("not our CA", timedelta(days=2))
    bad = foreign.issue_client(ROLE_ADMIN, "admin", min(lifetime, timedelta(days=1)))
    Issued(bad.cert, bad.key, (), root.cert).write_client_dir(clients / "foreign")
    root.issue_client(ROLE_WORKER, "ewkit-phase0", lifetime).write_client_dir(clients / "root-signed-worker")
    worker_ca.issue_client(ROLE_CONTROLLER, "controller", lifetime).write_client_dir(clients / "worker-ca-controller")


def cmd_show(_: Layout, args: argparse.Namespace) -> None:
    for path in args.path:
        files = sorted(p for p in path.rglob("*.pem") if "key" not in p.name) if path.is_dir() else [path]
        for f in files:
            for i, cert in enumerate(load_certs(f)):
                print(f"{f}{f'[{i}]' if i else ''}")
                print(f"  subject  {cert.subject.rfc4514_string()}")
                print(f"  issuer   {cert.issuer.rfc4514_string()}")
                print(f"  valid    {cert.not_valid_before_utc:%Y-%m-%d %H:%M} to {cert.not_valid_after_utc:%Y-%m-%d %H:%M} UTC"
                      f"{'  EXPIRED' if cert.not_valid_after_utc < now() else ''}")  # fmt: skip
                if names := sans(cert):
                    print(f"  SANs     {', '.join(names)}")


def sans(cert: x509.Certificate) -> list[str]:
    try:
        ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
    except x509.ExtensionNotFound:
        return []
    return [str(n.value) for n in ext]


def unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentic-ops-pki", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--dir", type=Path, default=Path(os.environ.get("AGENTIC_OPS_PKI_DIR", DEFAULT_DIR)),
                   help="CA directory (default: $AGENTIC_OPS_PKI_DIR or secrets/temporal-pki)")  # fmt: skip
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("root", help="create the root CA (once)")
    s.add_argument("--common-name", default="agentic-ops root CA")
    s.add_argument("--days", type=int, default=ROOT_DAYS)
    s.add_argument("--no-passphrase", action="store_true", help="leave the key unencrypted (tests only)")
    s.set_defaults(func=cmd_root)

    s = sub.add_parser("worker-ca", help="create or rotate the worker-only intermediate CA")
    s.add_argument("--common-name", default="agentic-ops worker CA")
    s.add_argument("--days", type=int, default=WORKER_CA_DAYS)
    s.add_argument("--rotate", action="store_true", help="replace an existing worker CA")
    s.set_defaults(func=cmd_worker_ca)

    s = sub.add_parser("server", help="issue the frontend's server certificate")
    s.add_argument("-n", "--name", action="append", default=[], help="DNS name (repeatable)")
    s.add_argument("-i", "--ip", action="append", default=[], help="IP address (repeatable)")
    s.add_argument("--common-name", default="temporal")
    s.add_argument("--days", type=int, default=SERVER_DAYS)
    s.set_defaults(func=cmd_server)

    s = sub.add_parser("client", help="issue an admin, controller or reader certificate from the root")
    s.add_argument("--role", required=True, choices=ROOT_SIGNED_ROLES)
    s.add_argument("--name", required=True, help="CN, and the directory name under clients/")
    s.add_argument("--days", type=int, help=f"default: {CLIENT_DAYS}")
    s.add_argument("--out", type=Path, help="default: DIR/clients/<name>")
    s.set_defaults(func=cmd_client)

    s = sub.add_parser("worker", help="sign a short-lived worker certificate with the worker CA")
    who = s.add_mutually_exclusive_group(required=True)
    who.add_argument("--production", help="CN=ewkit-<production>, for its unit workers")
    who.add_argument("--af-io", action="store_true", help=f"CN={AF_IO_NAME}")
    s.add_argument("--hours", type=float, default=WORKER_LIFETIME.total_seconds() / 3600)
    s.add_argument("--out", type=Path, help="default: DIR/clients/<CN>")
    s.set_defaults(func=cmd_worker)

    s = sub.add_parser("dev", help="unencrypted CA, server and every test client, for local tests")
    s.add_argument("-n", "--name", action="append", default=[], help="extra server DNS name")
    s.add_argument("-i", "--ip", action="append", default=[], help="extra server IP address")
    s.add_argument("--hours", type=float, default=7 * 24, help="lifetime of the test clients")
    s.set_defaults(func=cmd_dev)

    s = sub.add_parser("test-clients", help="short-lived clients for phase0/tests/test_authz.py from this CA")
    s.add_argument("--hours", type=float, default=8)
    s.add_argument("--out", type=Path, help="default: DIR/test (clients go in <out>/clients)")
    s.set_defaults(func=cmd_test_clients)

    s = sub.add_parser("show", help="print subject, issuer, validity and SANs")
    s.add_argument("path", nargs="+", type=Path)
    s.set_defaults(func=cmd_show)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        args.func(Layout(args.dir), args)
    except PKIError as e:
        print(f"agentic-ops-pki: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
