# agentic-ops-pki

Certificates for the agentic-ops Temporal server (phase 1, task 4 of
[the production plan](../docs/temporal-production-plan.md#phase-1-platform)): a command-line
tool for the operator, and a library the ops-executor uses to sign worker certificates.

## The scheme

Subjects are `O=agentic-ops, OU=<role>, CN=<name>`, as `temporal-server/authz` reads them.

| Certificate | Signed by | Lifetime | Where it lives |
|---|---|---|---|
| Root CA | itself | 10 years | `root/`; the key encrypted with a passphrase, kept offline |
| Worker CA (`OU=worker-ca`) | root | 1 year | `worker-ca/`; the ops-executor's Secret |
| Server (`OU=server`) | root | 1 year | `server/`; the server's Secret, with the root and the worker CAs |
| `admin` | root | 30 days | the operator's machine only |
| `controller` | root | 1 year | controller pods, ops-executor, backend |
| `reader` | root | 1 year | Web UI, read-only Temporal MCP server |
| `worker`: `CN=ewkit-<production>` or `CN=af-io` | worker CA | 30 hours | one per HTCondor job, in its scratch directory |

What protects what:

- The frontend trusts only the root (`clientCaFiles`); a worker sends its certificate followed
  by the worker CA.
- The claim mapper accepts `OU=worker` only from a certificate whose issuer is one of the
  worker CAs in `worker-ca.pem`, and every other role only from a certificate the root signed
  directly. A leaked worker CA key can mint workers (limited to their queues and productions by
  the authorizer), never a controller or an admin.
- The worker CA's extended key usage is `clientAuth` and its path length 0, so it cannot sign a
  server certificate that clients would accept, nor another CA.
- Temporal checks no revocation lists. Worker certificates expire on their own; to cut off a
  worker CA, rotate it and drop the old one from `worker-ca.pem` (below).

## Use

From this directory (`uv run` installs `cryptography`). The CA directory is `--dir`, or
`AGENTIC_OPS_PKI_DIR`, or `secrets/temporal-pki` in this repository. `--help` on each command
lists the options. The production commands are in
[charts/temporal/README.md](../charts/temporal/README.md#certificates).

Local tests (unencrypted, into `secrets/phase0-certs`, which `phase0/local/compose.yaml` and
`phase0/tests/test_authz.py` read):

```bash
uv run agentic-ops-pki --dir ../secrets/phase0-certs dev
```

Inspect what is there:

```bash
uv run agentic-ops-pki show ../secrets/temporal-pki/server ../secrets/temporal-pki/clients
```

### Rotating the worker CA

```bash
uv run agentic-ops-pki worker-ca --rotate && uv run agentic-ops-pki server -n temporal.servicex.ssl-hep.org -i 192.170.236.183
```

The old worker CA moves to `worker-ca/retired/` (its key is deleted) and stays in the server's
`worker-ca.pem` until it expires, so workers already running keep working. To cut it off
sooner, delete it from `retired/` and re-issue the server certificate. Either way, update both
Secrets and restart the server.

### Signing worker certificates in code

```python
from pathlib import Path
from agentic_ops_pki.ca import WorkerSigner

signer = WorkerSigner.from_dir(Path("/etc/agentic-ops/worker-ca"))  # ca.pem, ca-key.pem, root.pem
issued = signer.sign_production("p2026a")   # or signer.sign_af_io(); 30 hours by default
issued.cert_chain_pem, issued.key_pem, issued.ca_pem   # cert.pem, key.pem, ca.pem for the job
issued.write_client_dir(Path(scratch) / "certs")       # or write them, 0700/0600
```

## Tests

```bash
uv run pytest
```

They check every chain with `cryptography`'s path validation, including that the worker CA
cannot mint a server certificate or a sub-CA. The issuer checks in the server are covered by
the Go tests in `temporal-server/authz` and, against a running server, by
`phase0/tests/test_authz.py`.
