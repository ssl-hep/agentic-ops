"""The agentic-ops certificate scheme: an offline root, a worker-only intermediate, and leaves.

Subjects follow temporal-server/authz: O=agentic-ops, OU=<role>, CN=<name>.

- The **root** signs the server certificate, the admin, controller and reader certificates,
  and the worker CA. Its key stays on the operator's machine, encrypted.
- The **worker CA** signs only worker certificates (CN=ewkit-<production> or CN=af-io). The
  ops-executor holds its key and signs one per HTCondor submission. The claim mapper accepts
  the worker role only from a worker CA and the other roles only from the root, and the worker
  CA's extended key usage is clientAuth alone, so a leaked worker CA key can mint workers but
  not a controller, an admin or a server certificate.

A certificate directory for a client (what `connect` in phase0/shim/common.py and an
HTCondor job read) holds cert.pem (the leaf, then any intermediates), key.pem and ca.pem (the
root, to verify the server).
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ORGANIZATION = "agentic-ops"

ROLE_ADMIN = "admin"
ROLE_CONTROLLER = "controller"
ROLE_READER = "reader"
ROLE_WORKER = "worker"
ROOT_SIGNED_ROLES = (ROLE_ADMIN, ROLE_CONTROLLER, ROLE_READER)

AF_IO_NAME = "af-io"
# Kept free of "-" so one production's queues never match another's (temporal-server/authz)
PRODUCTION_ID = re.compile(r"[a-z0-9]{1,40}")

# A worker's 24 hours plus its last unit
WORKER_LIFETIME = timedelta(hours=30)
# Leaves are valid from a little in the past, for clocks that run behind
CLOCK_SKEW = timedelta(minutes=5)


class PKIError(Exception):
    pass


def worker_name(production: str) -> str:
    """The CN of the unit workers of one production."""
    if not PRODUCTION_ID.fullmatch(production):
        raise PKIError(f"production ID must match {PRODUCTION_ID.pattern}: {production!r}")
    return f"ewkit-{production}"


def check_worker_name(name: str) -> str:
    if name != AF_IO_NAME:
        production = name.removeprefix("ewkit-")
        if production == name or not PRODUCTION_ID.fullmatch(production):
            raise PKIError(f"worker name must be {AF_IO_NAME} or ewkit-<production>: {name!r}")
    return name


def subject(role: str | None, common_name: str) -> x509.Name:
    attrs = [x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORGANIZATION)]
    if role:
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, role))
    attrs.append(x509.NameAttribute(NameOID.COMMON_NAME, common_name))
    return x509.Name(attrs)


def new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Authority:
    """A CA certificate with its key, and the chain above it (empty for the root)."""

    cert: x509.Certificate
    key: ec.EllipticCurvePrivateKey
    chain: tuple[x509.Certificate, ...] = ()

    @property
    def root(self) -> x509.Certificate:
        return self.chain[-1] if self.chain else self.cert

    @property
    def not_after(self) -> datetime:
        return self.cert.not_valid_after_utc

    def sign(
        self,
        subject_name: x509.Name,
        public_key: ec.EllipticCurvePublicKey,
        *,
        not_after: datetime,
        extensions: list[tuple[x509.ExtensionType, bool]],
    ) -> x509.Certificate:
        if not_after > self.not_after:
            raise PKIError(
                f"{self.cert.subject.rfc4514_string()} expires {self.not_after:%Y-%m-%d %H:%M} UTC, "
                f"before the certificate would ({not_after:%Y-%m-%d %H:%M} UTC); renew or rotate it first"
            )
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject_name)
            .issuer_name(self.cert.subject)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(max(now() - CLOCK_SKEW, self.cert.not_valid_before_utc))
            .not_valid_after(not_after)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), critical=False)
        )
        for ext, critical in extensions:
            builder = builder.add_extension(ext, critical=critical)
        return builder.sign(self.key, hashes.SHA256())

    def issue_ca(self, common_name: str, role: str, lifetime: timedelta) -> Authority:
        """An intermediate that may sign client certificates only, and no further CAs."""
        key = new_key()
        cert = self.sign(
            subject(role, common_name),
            key.public_key(),
            not_after=now() + lifetime,
            extensions=[
                (x509.BasicConstraints(ca=True, path_length=0), True),
                (ca_key_usage(), True),
                (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), False),
            ],
        )
        return Authority(cert, key, (self.cert, *self.chain))

    def issue_client(self, role: str, common_name: str, lifetime: timedelta) -> Issued:
        key = new_key()
        cert = self.sign(
            subject(role, common_name),
            key.public_key(),
            not_after=now() + lifetime,
            extensions=[
                (x509.BasicConstraints(ca=False, path_length=None), True),
                (leaf_key_usage(), True),
                (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), False),
            ],
        )
        return Issued(cert, key, self.chain_from_issuer(), self.root)

    def issue_server(self, common_name: str, dns_names: list[str], ips: list[str], lifetime: timedelta) -> Issued:
        key = new_key()
        sans: list[x509.GeneralName] = [x509.DNSName(n) for n in dns_names]
        sans += [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
        cert = self.sign(
            subject("server", common_name),
            key.public_key(),
            not_after=now() + lifetime,
            extensions=[
                (x509.BasicConstraints(ca=False, path_length=None), True),
                (leaf_key_usage(), True),
                (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False),
                (x509.SubjectAlternativeName(sans), False),
            ],
        )
        return Issued(cert, key, self.chain_from_issuer(), self.root)

    def chain_from_issuer(self) -> tuple[x509.Certificate, ...]:
        """What a leaf sends after itself: this CA and the intermediates above it, not the root."""
        return () if not self.chain else (self.cert, *self.chain[:-1])


def ca_key_usage() -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=False, content_commitment=False, key_encipherment=False,
        data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
        encipher_only=False, decipher_only=False,
    )  # fmt: skip


def leaf_key_usage() -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=True, content_commitment=False, key_encipherment=False,
        data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
        encipher_only=False, decipher_only=False,
    )  # fmt: skip


def new_root(common_name: str, lifetime: timedelta) -> Authority:
    key = new_key()
    name = subject(None, common_name)
    start = now() - CLOCK_SKEW
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + lifetime)
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(ca_key_usage(), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return Authority(cert, key)


@dataclass(frozen=True)
class Issued:
    """A leaf certificate, its key, the intermediates it sends, and the root to trust."""

    cert: x509.Certificate
    key: ec.EllipticCurvePrivateKey
    intermediates: tuple[x509.Certificate, ...]
    root: x509.Certificate

    @property
    def cert_chain_pem(self) -> bytes:
        return b"".join(pem(c) for c in (self.cert, *self.intermediates))

    @property
    def key_pem(self) -> bytes:
        return key_pem(self.key)

    @property
    def ca_pem(self) -> bytes:
        return pem(self.root)

    def write_client_dir(self, path: Path) -> Path:
        """cert.pem, key.pem and ca.pem in a directory only the owner can read."""
        write_private_dir(path)
        write_file(path / "cert.pem", self.cert_chain_pem)
        write_file(path / "key.pem", self.key_pem)
        write_file(path / "ca.pem", self.ca_pem)
        return path


class WorkerSigner:
    """Signs worker certificates with the worker CA: what the ops-executor does per submission.

    Reads a worker CA directory (`WorkerSigner.from_dir`): ca.pem, ca-key.pem and root.pem.
    """

    def __init__(self, authority: Authority):
        if authority.cert.extensions.get_extension_for_class(x509.BasicConstraints).value.path_length != 0:
            raise PKIError("not a worker CA")
        self.authority = authority

    @classmethod
    def from_dir(cls, path: Path) -> WorkerSigner:
        return cls(load_authority(path / "ca.pem", path / "ca-key.pem", chain_file=path / "root.pem"))

    def sign(self, name: str, lifetime: timedelta = WORKER_LIFETIME) -> Issued:
        return self.authority.issue_client(ROLE_WORKER, check_worker_name(name), lifetime)

    def sign_production(self, production: str, lifetime: timedelta = WORKER_LIFETIME) -> Issued:
        return self.sign(worker_name(production), lifetime)

    def sign_af_io(self, lifetime: timedelta = WORKER_LIFETIME) -> Issued:
        return self.sign(AF_IO_NAME, lifetime)


# Files


def pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def key_pem(key: ec.EllipticCurvePrivateKey, passphrase: bytes | None = None) -> bytes:
    encryption = serialization.BestAvailableEncryption(passphrase) if passphrase else serialization.NoEncryption()
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)


def write_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def write_file(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write through a temporary file, so a reader never sees half a key, with mode set first."""
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    tmp.chmod(mode)
    tmp.replace(path)


def load_certs(path: Path) -> list[x509.Certificate]:
    return x509.load_pem_x509_certificates(path.read_bytes())


def load_authority(
    cert_file: Path, key_file: Path, *, chain_file: Path | None = None, passphrase: bytes | None = None
) -> Authority:
    cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
    key = serialization.load_pem_private_key(key_file.read_bytes(), passphrase)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.public_key() != cert.public_key():
        raise PKIError(f"{key_file} is not the key of {cert_file}")
    chain = tuple(load_certs(chain_file)) if chain_file else ()
    return Authority(cert, key, chain)


def key_is_encrypted(key_file: Path) -> bool:
    return b"ENCRYPTED" in key_file.read_bytes()[:64]
