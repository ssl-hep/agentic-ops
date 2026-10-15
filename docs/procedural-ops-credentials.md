# Grid credentials for procedural ops

Status: design, nothing built yet. Records the decision taken on D1 in
[the proposal](agentic-ops-proposal.html#decisions-list).

## Why

From Gate 1 on, the service has to act at the facility. It resolves datasets
in Rucio, submits and watches HTCondor jobs, holds, releases and removes them,
and checks files over XRootD. All of these need a grid identity. We use a
project-owned **robot X509 certificate** (for a VOMS proxy) and/or a
**Kerberos service keytab**, and the backend holds it.

The agents must never be able to reach that credential. They can't today, but
only by accident of what has been built so far:

- The agents run in-process in the single uvicorn worker. The graph is invoked
  inside the request (`backend/app/setup_api.py:185`, `:209`), and the models,
  repository and settings are built in the lifespan
  (`backend/app/main.py:19`).
- Every secret in that process is a `SecretStr` on `Settings`
  (`backend/app/settings.py`). The whole `Settings` object travels in
  `SetupContext` (`backend/app/agents/graph.py:40`), so it is one attribute
  away from any node.
- The tool loop sends exception text straight back to the model
  (`backend/app/agents/llm.py:68`, `f"Tool error: {exc}"`).
- The Helm chart runs one container with secrets as env vars. It has no
  `securityContext`, ServiceAccount, RBAC or NetworkPolicy
  (`charts/agentic-ops/templates/backend-deployment.yaml`).

If a proxy or keytab were put into that pod as an env var or a file, then one
future tool would put it within reach of the model. That tool could be a shell,
a file read, an MCP adapter, or a tool whose error message includes a path. The
agents' tools are read-only today (`backend/app/agents/tools.py:51`). That
holds because of how the code is written now, not because anything enforces it.

Nothing grid-facing exists yet: the validator says Rucio and HTCondor checks
"come later" (`backend/app/validator.py:4`). So we can draw the boundary
before any credential code is written.

## Principle

> The credential lives in a different pod, under a different identity. Only
> deterministic code can talk to that pod, and only through a closed catalogue
> of typed operations.

None of these ever contains the credential, a path to it, or anything derived
from it:

- the agent process (its env, filesystem and `Settings`)
- graph state, run context, checkpoints, `history`
- logs and MLflow traces

This extends the rule the proposal already sets for every action: the model
proposes, a person approves, and deterministic code executes.

## Components

```
 web ──► agent-api  (backend pod, uid 10001)            ops-executor pod  (own ServiceAccount)
         ├─ LangGraph nodes that call a model           ┌───────────────────────────────────────┐
         │    builder, reviewer, monitor diagnosis      │ cred-renewer   (sidecar, own uid)     │
         │    tools: read-only only                     │   mounts Secret: robot cert+key,      │
         │    no OpsClient in their context             │                  service keytab       │
         │                                              │   kinit -k -t / voms-proxy-init       │
         └─ execution node (no model)  ──── mTLS ────►  │   writes short-lived proxy + ccache   │
              OpsClient: one typed method per op        │          ▼                            │
              runs only after interrupt() approval      │   emptyDir {medium: Memory}           │
                                                        │          ▼ (read-only)                │
                                                        │ executor       (own uid)              │
                                                        │   POST /ops/{op}, typed requests      │
                                                        │   argv templates, shell=False         │
                                                        │   rucio, condor_*, xrdfs              │
                                                        └───────────┬───────────────────────────┘
                                                                    ▼ egress: grid endpoints only
                                                       Rucio · schedd · XRootD · KDC · VOMS
```

### ops-executor (new Deployment)

The executor gets its own image with the grid clients: `htcondor`,
`rucio-clients`, `xrootd-client`, `voms-clients` and `krb5-user`. The backend
image stays free of grid tooling and grid config.

The pod has two containers.

**cred-renewer** (sidecar) is the only container that mounts the long-lived
Secret, holding `usercert.pem` and `userkey.pem`, `service.keytab`, or both. On
a timer it runs:

- `kinit -k -t service.keytab <principal>`, or `k5start`, which keeps a
  ccache renewed
- `voms-proxy-init -voms atlas -valid 12:00 -cert ... -key ...`

It writes the **short-lived** proxy and ccache to a shared
`emptyDir {medium: Memory}` volume. It also reports the time left on each, for
the pod's health check.

**executor** mounts that in-memory volume **read-only** and never sees the key
or keytab. If the executor is compromised, what leaks is a credential that
lasts hours. It works as follows:

- `X509_USER_PROXY` and `KRB5CCNAME` are set only in the env it passes to its
  own child processes.
- Each op is a Pydantic request model (`extra="forbid"`) mapped to a fixed argv
  template, run with `subprocess.run([...], shell=False, env=<allowlist>)`.
- There is no "run command", "read file" or passthrough op, and nothing takes
  a free-form path or argument string.
- Policy is checked per op on the server side: allowed Rucio scopes, the one
  schedd, the job owner/accounting tag, and the maximum number of jobs or files
  per call.
- Each op is marked `read` or `write`.

### OpsClient (in agent-api)

`OpsClient` is a typed client with one method per catalogue op. It follows the
pattern of `ReadOnlyGitLabClient` (`backend/app/gitlab.py:25`), which can only
issue GETs.

- It is built in the lifespan, but it is **only** passed to the deterministic
  execution node (and to the validator for read ops at Gate 1).
- It is never put on the context used by nodes that call a model, and never
  wrapped as a `StructuredTool`.
- Its own auth secret (mTLS client cert or bearer) allows it to *call
  catalogue ops*. It gives no access to the grid.

### Op catalogue (sketch)

| Op | Kind | Arguments (typed) | Returns |
|---|---|---|---|
| `rucio_list_replicas` | read | DID list, RSE expression | replicas per DID, file counts, sizes |
| `rucio_did_meta` | read | DID | events, size, availability |
| `xrd_stat` | read | file DID / logical name (not a URL) | exists, size |
| `condor_q` | read | cluster ids | per-job state, hold reason code |
| `condor_history` | read | cluster ids | exit codes, runtimes |
| `condor_submit` | write | RunSpec id, `approval_id` | cluster id, job count |
| `condor_hold` / `condor_release` / `condor_rm` | write | cluster ids, `approval_id` | per-job outcome |

The executor builds the submit description from the RunSpec, with
`use_x509userproxy = true` pointing at its own proxy. The submit file is never
built from text written by the model.

## Layers that keep agents out

| Layer | Mechanism |
|---|---|
| Process / pod | The credential Secret is referenced only by the ops-executor Deployment. agent-api gets no volume, no env var and no `Settings` field for it. |
| Kubernetes identity | Separate ServiceAccounts. `automountServiceAccountToken: false` on agent-api, and no RBAC that grants `get secrets`. Distinct UIDs, `runAsNonRoot`, `readOnlyRootFilesystem`, all capabilities dropped. |
| Network | NetworkPolicy: ops-executor accepts ingress only from agent-api pods, on one port. Its egress goes only to grid endpoints (Rucio, schedd, XRootD, KDC, VOMS), never to the model API. agent-api has no route to grid endpoints. |
| Caller auth | mTLS, or a bearer, between agent-api and the executor. Leaking it lets someone call catalogue ops, not use the grid identity. |
| Interface | Closed catalogue, typed arguments, argv templates, no shell, no file reads, allowlisted env. |
| Approval | Write ops are called only from the execution node after a LangGraph `interrupt()` approval (same pattern as `await_person`, `backend/app/agents/graph.py:177`). As defence in depth, the executor requires an `approval_id` on write ops. It checks the ID against the approvals table through a **read-only** Postgres role: the approval must exist, be by a person, be unused, and match the op and arguments. |
| Output | The executor returns **parsed structured results** (ids, states, hold reason codes, sizes), not raw stdout or stderr. A redaction pass strips PEM blocks, `x509up_*` and `krb5cc_*` paths, the values of `X509_*`/`KRB5*`, and the robot DN, then truncates. Anything shown to a monitor agent is wrapped with `evidence()` (`backend/app/agents/tools.py:19`). |
| Errors | `OpsClient` raises `OpsError(op, code)` with no free text from the executor, so the `Tool error: {exc}` path (`backend/app/agents/llm.py:68`) has nothing sensitive to echo. Fix that path anyway, so it reports the error class and a safe message rather than `str(exc)`. |
| State | No credential material, paths or DN in graph state, checkpoints, `history`, logs or MLflow traces. Approval records hold the op and arguments, not the identity used. |
| Local dev and harness | Nothing grid-related goes in the repo-root `.env`. `harness/agent_harness/config.py:32` loads that whole file into `os.environ`, next to the agent loop. Locally, the executor runs as its own process or container with its own env file. |

## Guard tests (to write when this is built)

- `backend`:
  - `Settings` has no field matching `x509|proxy|keytab|krb|voms|grid`.
  - The app's environment has no `X509_*` or `KRB5*` at startup.
- `backend`:
  - `repository_tools` (`backend/app/agents/tools.py:51`) and any monitor tool
    list contain no `OpsClient` methods.
  - The contexts of nodes that call a model carry no `ops` attribute.
- `ops-executor`:
  - Unknown ops are rejected, as are extra fields and write ops without a
    valid, unused `approval_id`.
  - Every op's argv contains only template constants and validated fields.
- `ops-executor`: redaction catches a PEM block, a proxy path, a ccache path
  and the DN in both stdout and stderr fixtures.
- Helm: a template test checks that only the ops-executor Deployment
  references the credential Secret, and that only cred-renewer mounts it.

## Lifecycle

- **Ownership and rotation.** The robot certificate expires yearly, and the
  keytab changes whenever the principal's password does. Name an owner, as for
  the EWKit token (D5).
- **Health.**
  - The executor's readiness check fails when proxy or ticket time left drops
    below a threshold.
  - An alert fires well before that.
  - When the monitor sees "proxy expired" hold reasons, it checks the renewer's
    health before it proposes anything else.
- **Accounting.**
  - Jobs run as the robot identity, so per-user accounting and quota at the
    facility are lost unless every job carries the requesting user. Do that
    with `+AccountingGroup`, or a custom ClassAd set by the executor from the
    approval record, never from model output.
  - Running as a service account needs facility sign-off.
- **Revocation.** Deleting the Secret and restarting the pod stops all grid
  access within the proxy lifetime. Revoking at the CA or KDC stops it at
  once.

## Open items

- mTLS through a mesh, or a chart-generated bearer: pick one when the chart
  grows a second Deployment.
- Whether HTCondor access goes through the AF MCP gateway (D2). If it does, the
  same executor holds the gateway token instead of, or as well as, a proxy. The
  isolation design is the same.
- Per-user delegation, the D1 lean, is still possible later. It is the same
  executor, choosing a credential per request from a store only it can read.
