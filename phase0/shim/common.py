"""Connecting to the phase 0 server with one of the certificates from certs/mkcerts.sh.

A certificate directory holds cert.pem, key.pem and ca.pem (clients/<name>/ in the
mkcerts output). The same layout is what an HTCondor job receives.
"""

import argparse
import os
from pathlib import Path

from temporalio.client import Client
from temporalio.service import TLSConfig

# PHASE0_NAMESPACE=ewkit-production runs the same tests and jobs against the phase 1 server
NAMESPACE = os.environ.get("PHASE0_NAMESPACE", "phase0")
CONTROLLER_QUEUE = "controller"
# Unit workers of production "phase0" poll ewkit-phase0-<memclass>; see temporal-server/authz.
UNIT_QUEUE = "ewkit-phase0-default"
AF_IO_QUEUE = "af-io"


def tls_config(cert_dir: Path, server_name: str, *, with_client_cert: bool = True) -> TLSConfig:
    return TLSConfig(
        server_root_ca_cert=(cert_dir / "ca.pem").read_bytes(),
        client_cert=(cert_dir / "cert.pem").read_bytes() if with_client_cert else None,
        client_private_key=(cert_dir / "key.pem").read_bytes() if with_client_cert else None,
        domain=server_name,
    )


async def connect(
    address: str,
    cert_dir: Path,
    server_name: str,
    *,
    namespace: str = NAMESPACE,
    identity: str | None = None,
    with_client_cert: bool = True,
) -> Client:
    return await Client.connect(
        address,
        namespace=namespace,
        tls=tls_config(cert_dir, server_name, with_client_cert=with_client_cert),
        identity=identity or f"{os.getpid()}@{os.uname().nodename}",
    )


def add_connection_args(parser: argparse.ArgumentParser, default_cert_dir: str | None = None) -> None:
    """--address, --server-name and --certs, defaulting to the PHASE0_* environment."""
    parser.add_argument("--address", default=os.environ.get("PHASE0_ADDRESS", "localhost:7233"))
    parser.add_argument(
        "--server-name",
        default=os.environ.get("PHASE0_SERVER_NAME"),
        help="name to verify the server certificate against (default: the host in --address)",
    )
    parser.add_argument(
        "--certs",
        type=Path,
        default=default_cert_dir,
        required=default_cert_dir is None,
        help="client certificate directory with cert.pem, key.pem and ca.pem",
    )


def server_name(args: argparse.Namespace) -> str:
    return args.server_name or args.address.rsplit(":", 1)[0]


def certs_root() -> Path:
    """The mkcerts output directory: PHASE0_CERTS, or secrets/phase0-certs in this repo."""
    default = Path(__file__).resolve().parents[2] / "secrets" / "phase0-certs"
    return Path(os.environ.get("PHASE0_CERTS", default))
