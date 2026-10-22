# EWKit ntuple production on Temporal: architecture plan

Status: proposal, nothing built yet. Written 2026-10-22.

This plan describes how agentic-ops automates EWKit ntuple production end to end: an analyst
narrows a dataset list with the agent, approves it, Rucio rules stage the DAODs to MWT2, HTCondor
workers on the UChicago AF process them, failures get triaged with agent-proposed fixes, and
reliable bookkeeping shows exactly what work remains.

It replaces two earlier designs:

- the LangGraph orchestrator, Postgres checkpointer and HTTP ops-executor in
  `docs/agentic-ops-proposal.html` (EWKit v1.1 draft, on the `sampledata` branch)
- a Dagster design (assets partitioned by production × dataset, Dagster Pipes into HTCondor)
  that was considered and dropped; see [Why not Dagster](#why-not-dagster)

## Requirements

1. One HTCondor worker processes work from many datasets.
2. Worker code comes from the EWKit repository at a pinned commit, not from an apptainer image.
3. Productions run for days to a week or more. Nothing may depend on a process staying alive
   for that long.
4. Failed work is re-run reliably, and at any moment we can say exactly what remains, per
   production and per dataset.
5. An agent proposes fixes for failures. People approve anything that changes physics or
   spends quota. Agents never hold grid credentials (decision D1 in
   `docs/procedural-ops-credentials.md` on the `mlflow` branch).
6. The Temporal server and its database run on Kubernetes. AF HTCondor workers reach gRPC
   endpoints on the internet (verified).

## Architecture overview

```
 analyst ── TUI (Textual) ──HTTPS──▶ backend (FastAPI) ──▶ Postgres: selections, approvals, reporting
                                       │  narrowing agent, triage agent (read-only tools)
                                       │  Temporal client: start production, send decisions (Updates)
                                       ▼
                          Temporal server (k8s) ◀── Postgres: temporal, temporal_visibility
                            ▲      ▲        ▲
              gRPC (mTLS)   │      │        │  gRPC (mTLS, public ingress)
     ┌──────────────────────┘      │        └──────────────────────────────┐
     │ k8s worker pods             │                                       │ AF HTCondor jobs
     │  controller queue           │ ops-executor pod                      │  queue ewkit-<prod>-<mem>
     │   planning, verification,   │  queue ops                            │  run_unit: EWRun.py on
     │   reporting                 │  robot x509 + Rucio + condor creds    │  a chunk of files,
     │  triage queue               │  ensure_rule, list_rules,             │  heartbeats, manifest
     │   LLM key, read-only tools  │  condor_submit/rm, extend_rule        │
     └─────────────────────────────┴───────────────────────────────────────┘
```

The pieces:

| Component | Runs on | Role |
|---|---|---|
| Temporal server | k8s (Helm) | Durable workflow state, task queues, timers, history, Web UI |
| Postgres | k8s | Temporal persistence and visibility; agentic-ops schema for selections, approvals, snapshots and reporting |
| backend | k8s, existing FastAPI app | Selection and agent APIs, approval API, Temporal client. The only place users act from |
| TUI | AF login node or laptop | Thin client of the backend: narrow, approve, watch, decide |
| controller workers | k8s pods | Workflows, plus activities that need no credentials: planning, verification, reporting |
| ops-executor | k8s pod with a credential sidecar | The only holder of the robot x509, Rucio and HTCondor credentials. Polls `ops` only |
| triage workers | k8s pods | The triage agent: LLM key and read-only tools. Polls `triage` only |
| HTCondor workers | AF worker nodes | Poll `ewkit-<production>-<memclass>`, run units, heartbeat, write outputs |

## Core concepts

**Production.** One immutable combination of analysis folder, config name, EWKit commit SHA, build
tarball digest and output tag, plus the approved selection it runs over. Code is never changed
inside a production. A fix that needs new code means a new production, which can reuse finished
work only if a person explicitly approves the two as compatible.

**Selection.** The approved list of Rucio DIDs, kept in Postgres with a content hash, the user
who approved it and the totals they saw (bytes against quota, estimated core-hours). Workflows
receive a `selection_id`, never the list itself. A 3.4k-DID list is about 350 KB; Temporal's
default payload limit is 2 MB per payload and every payload is kept in history, so passing the
list around would bloat histories for no gain.

**Snapshot.** When a dataset's replica is planned, every input file (GUID, LFN, size, checksum,
event count from Rucio) is frozen into `dataset_snapshots`. Coverage and event-count checks are
made against it.

**Unit.** A chunk of files from one dataset, sized to run for a few hours. Units are what workers
process; one worker processes many units, from any datasets of its production. Large data
containers are split by files, never processed as a whole.

**Attempt.** One try at a unit. Output paths include the attempt number, so a late finisher can
never overwrite or double-count the attempt that was accepted.

## Workflows

Workflow IDs are deterministic, so starting a workflow that already exists is a no-op:
`production/<production_id>`, `dataset/<production_id>/<did>`, `replica-watcher/<account>/<rse>`,
`pool-keeper/<production_id>`.

### ProductionWorkflow

Input: `production_id`.

1. `build_ewkit` (activity, runs as a short HTCondor job through `ops`): check out EWKit at the
   SHA, `asetup AnalysisBase,25.2.113`, build, and pack the InstallArea plus the worker
   environment into a tarball. Record its digest on the production.
2. Smoke test: run one unit per campaign with `-e 1000`. Wait for the launch approval
   (an Update from the backend).
3. Start one child `DatasetWorkflow` per dataset, at most N running at once, in batches so the
   parent's history stays small. Use continue-as-new between batches.
4. Start or update `PoolKeeperWorkflow` for this production.
5. When every dataset is complete: `write_ntuple_lists` (the `data/<A>/ntuples/*.txt` files
   `get_ntuples.py` produces today), `release_rules`, mark the production complete.

Handles: `cancel` (cascades to children and running activities, then `condor_rm` by production
tag), `pause`/`resume` (the pool keeper stops submitting), and a failure-batch signal from
datasets (see [Triage](#failure-handling-and-triage)).

### DatasetWorkflow

Input: `production_id`, DID.

1. `ensure_rule` on `ops`: create the rule on `MWT2_UC_LOCALGROUPDISK`, or adopt an existing
   one for the same account, DID and RSE. `InsufficientAccountLimit` is non-retryable and goes
   to a person.
2. Wait for replica signals from the watcher. Plan units as soon as all of their files are
   locked `OK`, so processing overlaps with transfer.
3. Run each unit (`run_unit` on `ewkit-<production>-<memclass>`), then `verify_unit`.
4. Handle permanent failures through the triage loop.
5. When every unit is `DONE` or approved `EXCLUDED`: record the dataset as complete with its
   events, sum of weights, output list and exclusions, and tell the parent.
6. Rule lifetime timer: one day before `expires_at`, if the dataset is not complete,
   `extend_rule` on `ops`.

Search attributes: `ProductionId`, `Dataset`, `DatasetState`, `UnitsDone`, `UnitsTotal`,
`ReplicaFilesOk`, `ReplicaFilesTotal`, `NeedsDecision`.

### ReplicaWatcherWorkflow

One per account and RSE. Every 15–30 minutes, `list_rules` on `ops` fetches all of the robot
account's rules on the RSE with their lock counts in one call. It signals a dataset workflow only
when its rule changes: progress, `OK`, `STUCK`, `SUSPENDED`. It also applies stall rules:

- no increase in files `OK` for N hours → triage
- `STUCK` for more than 24 hours (Rucio's judge-repairer retries first) → triage
- sources only on tape → longer allowance and an estimated arrival time instead of an alarm

Runs indefinitely, with continue-as-new.

### PoolKeeperWorkflow

One per production. Every few minutes it compares each task queue's backlog (Temporal's task
queue statistics) and running workers with targets, and calls `condor_submit` on `ops` for more
workers, with the matching memory request per `memclass` queue. It stops when the production is
paused, cancelled or done, and removes idle workers at the end.

## The HTCondor worker

### What runs in the job

Each job receives two tarballs through `transfer_input_files`: the EWKit build for the production
and a self-contained worker Python environment (python-build-standalone plus `temporalio`, built
for EL9). It:

1. Unpacks both into `$_CONDOR_SCRATCH_DIR`.
2. Starts the worker shim in the bundled Python. The shim connects to Temporal with the
   production's client certificate and polls `ewkit-<production>-<memclass>` with one activity
   slot per requested core.
3. For each unit, runs EWRun in a subprocess under ALRB from cvmfs and the build's `setup.sh`:
   `EWRun.py --analysis A --configName C -i inputs.txt -o <unit>.a<n>.root --merge-output-files`.
4. Heartbeats every minute with events processed so far. A watchdog kills EWRun if the output
   stops growing or the unit exceeds a hard time limit.
5. Writes the output and `manifest/<unit>.a<n>.json` (temp file, then rename).
6. Stops polling when the remaining walltime is less than the expected unit runtime, and exits
   cleanly. The pool keeper replaces it.

### Code in EWKit

The worker code lives in EWKit, split so the core has no Temporal dependency:

| File | Purpose |
|---|---|
| `python/EWUnit.py` | `run_unit(spec) -> manifest`: runs EWRun on a unit spec and writes the manifest. No Temporal import |
| `share/EWWorker.py` | The Temporal shim: activity definition, heartbeats, walltime logic. Imports `temporalio` |
| `share/build_worker_tarball.sh` | Builds the InstallArea tarball and the worker environment for a SHA |

Unit spec (JSON): unit id, attempt, production, analysis, config name, tag, DID, file list
(paths or `root://` URLs), output directory, events expected.

Manifest (JSON): unit id, attempt, status, exit code, events processed, sum of weights,
cutflow, output path, size, checksum, host, start and end times, log path.

### Fixes needed in EWKit

- `rtag_mc_map()` lacks r17610 (38 DiTauStudies datasets); naming raises `ValueError`.
- `get_datasets` strips only `\n`: CRLF and trailing spaces exist in several lists.
- `Extend:` configs write `config_extended.yaml` into the data directory; the job must copy
  the config to a writable directory first.
- `SampleNaming.check_tag()` needs a tagged git checkout; the worker passes the production's
  tag explicitly, like `--submissionTag`.
- `EWCondorRun.py` assumes a shared build directory and local sample directories. The new
  worker takes explicit file lists and the transferred tarball instead; `EWCondorRun.py` can
  stay for manual use.

## Bookkeeping

Temporal holds coordination state. Postgres (schema `production`) holds a reporting projection
that activities write to and the TUI reads, plus the data Temporal is bad at aggregating.

| Table | Contents |
|---|---|
| `productions` | Analysis, config, SHA, tarball digest, tag, selection, state, approvals |
| `dataset_snapshots` | Per file: DID, GUID, LFN, size, checksum, events, snapshot time |
| `units` | Production, DID, file list, state, attempt count, failure count, memclass, parent unit (after a split) |
| `attempts` | Append-only: unit, attempt, worker, condor `cluster.proc`, host, times, outcome, error class, output path, manifest |
| `decisions` | Append-only: proposal, who decided, decision, outcome. Mirrors the Updates in workflow history |

Unit states: `PLANNED`, `QUEUED`, `RUNNING`, `VERIFYING`, `DONE`, `RETRY_WAIT`, `BLOCKED`,
`NEEDS_DECISION`, `EXCLUDED`, `SUPERSEDED`, `CANCELLED`.

### Verification

`verify_unit` accepts an attempt only if:

- the output exists at its expected path and its size and checksum match the manifest
- the tree and the CutBookkeeper histograms are present (uproot; no ROOT needed)
- events processed equals the sum of Rucio event counts for the unit's input files
- sum of weights is above zero for MC

Accepting an attempt fixes it as the one that counts. Any later output for the same unit is
recorded as a duplicate and deleted by cleanup.

### Invariants

Checked continuously by a reporting activity and exposed in the TUI:

- each snapshot file is in exactly one live unit (not `SUPERSEDED`), unless `EXCLUDED` with an
  approval on record
- a dataset is complete only when every unit is `DONE` or approved `EXCLUDED`
- no unit has more than one accepted attempt
- snapshot drift: a periodic comparison with Rucio flags files added to a container after
  planning (whole-year data containers can grow). New files become units only with approval

### What remains

One query over `units`, grouped by production, dataset and state, excluding `DONE`, `EXCLUDED`,
`SUPERSEDED` and `CANCELLED`. Multiplied by the median unit runtime, it estimates core-hours left.
Replica progress comes from the same tables via the watcher, so the TUI can show transfer and
processing progress side by side.

## Failure handling and triage

### Retry policy

| Class | Examples | Handling |
|---|---|---|
| infrastructure | eviction, heartbeat timeout, node failure | Temporal retries automatically (heartbeat timeout 10 min) |
| transient input | xrootd timeout, stage-in error | Retry with backoff; after N tries, `BLOCKED` and check the replica |
| resources | out of memory, walltime | One retry on the next `memclass` queue, or propose a split |
| bad input | same file fails on two hosts | Non-retryable; proposal to exclude the file |
| code or config | traceback, same crash twice | Non-retryable; proposal for a new production |

`run_unit` declares `non_retryable_error_types` for the last two classes, so the agent only sees
failures Temporal's own retries could not fix.

### Triage loop

When a unit fails permanently, the dataset workflow:

1. runs `collect_evidence` on `controller`: log tail, exit code, host, hold reason, events
   processed (all attached to the failure by a worker interceptor), replica state of the inputs,
   and similar failures elsewhere in the production
2. matches known signatures with fixed rules (xrootd `[3010]`, `std::bad_alloc`, corrupt `TFile`)
   and only calls the agent for failures nothing recognises
3. runs `triage` on the `triage` queue, which returns a typed proposal: an action from a closed
   set (`retry`, `retry_with_memory`, `split`, `exclude_files`, `new_production`, `escalate`),
   parameters, rationale and evidence references
4. sets `NeedsDecision`, waits for a `decide` Update, and applies the decision deterministically

The Update's validator rejects actions outside the allowed set or bounds (memory cap, maximum
files excluded). A policy table, kept outside workflow code, lists actions that are approved
automatically, such as one memory increase after an out-of-memory error. Excluding files and new
productions always need a person.

Batching: dataset workflows signal failure fingerprints to their production workflow, which
groups them over a 15-minute window and triages each group once. Group-level actions include
pausing the pool keeper and excluding a host through a condor `Requirements` change.

Transfer failures use the same loop with different evidence (rule error, stuck files, source
replica locations and site downtimes, quota) and actions: wait, `boost_rule`, a new rule with a
different `source_replica_expression`, read the dataset through XCache instead (decision D11's
fallback), exclude stuck files, or escalate to DDM ops with a drafted ticket.

### The agent

- Start with a plain activity that calls a LangChain agent, like the existing narrower, with
  MLflow tracing. Move to a durable agent workflow through one of Temporal's agent SDK plugins
  only if triage turns into long investigations.
- Job logs are untrusted input; wrap them in `<evidence>` tags as the narrower's tools do.
- The triage pod has the LLM key and read-only tools only. It never polls `ops` and has no
  Temporal client.
- Evaluation: every proposal, decision and outcome (did the retry work?) is exported to an MLflow
  evaluation dataset, the same way as the narrower's eval harness.
- Operators can also inspect workflow histories interactively through a read-only Temporal MCP
  server.

## Credentials and security

The credential isolation decision (D1) maps onto task queues:

| Task queue | Polled by | Credentials |
|---|---|---|
| `ops` | ops-executor pod only | Robot x509 proxy (renewed by a sidecar from a long-lived Secret), Rucio account, HTCondor submit token |
| `controller` | controller pods | Database role for the reporting schema; read access to outputs |
| `triage` | triage pods | LLM key, MCP bearer token for read-only Rucio tools, MLflow |
| `ewkit-<production>-<memclass>` | HTCondor jobs | A short-lived client certificate for that production |

The server is reachable from the internet, so:

- **mTLS on the frontend.** A dedicated CA issues per-production worker certificates that expire
  when the production's deadline passes. The pool keeper hands each job its certificate through
  HTCondor's credential handling, never on a shared filesystem.
- **Authorization.** Open-source Temporal checks permissions per namespace, not per task queue.
  A worker certificate must not be able to start, signal or cancel workflows. Options, to settle
  in phase 0: a custom authorizer and claim mapper that limits worker identities to poll and
  respond calls on their own queues, or a separate frontend endpoint for workers with that
  restriction.
- **Only the frontend is exposed,** through a gRPC-capable ingress (HTTP/2, TLS passthrough or
  re-encrypt). The Web UI and admin tools stay behind CERN SSO (oauth2-proxy) on an internal
  address.
- **Users never talk to Temporal directly.** The TUI calls the backend, which checks the user's
  identity and the approvals table before starting workflows or sending Updates.
- **Accounting.** Jobs carry `+AccountingGroup` for the requesting user. Needs facility sign-off.

## Deployment

- Namespace `agentic-ops` on the chosen k8s cluster.
- Temporal from the `temporalio/helm-charts` chart with its bundled database disabled, pointed
  at Postgres databases `temporal` and `temporal_visibility` (SQL advanced visibility, so custom
  search attributes work). Postgres from the existing chart or an operator such as
  CloudNativePG, with scheduled backups. The agentic-ops schema lives on the same server.
- Temporal namespace `ewkit-production`, retention long enough to cover a production plus
  review (30 days to start), with the search attributes above registered.
- Worker Deployments (controller, ops-executor, triage) in the existing agentic-ops chart, each
  with its own ServiceAccount, NetworkPolicy and Secrets.
- Worker versioning, or `workflow.patched()` for every change to workflow code, because
  productions run for longer than the gap between deployments.
- Prometheus scraping of the server and SDK metrics; alerts on schedule-to-start latency,
  `NeedsDecision` older than 24 hours, stalled replicas, and server health.

## Repository changes

agentic-ops:

- `worker/`: a new Python package with workflows, controller activities, ops activities and
  triage activities. Separate entry points per task queue.
- `backend/`: selections move from the in-memory `SelectionStore` to Postgres; new approval and
  production endpoints; a Temporal client; the triage agent next to the narrower.
- `tui/`: a Textual app over the backend API: dataset table with facets, chat with the
  narrowing agent and a diff of its proposals, approval screen with totals, production and
  replica progress, triage queue.
- `charts/agentic-ops/`: worker Deployments, ops-executor with the credential sidecar,
  Temporal as a dependency or a sibling release.
- Removed: the LangGraph run-setup stack on `sampledata`/`submit` is not carried forward. The
  Next.js frontend keeps the campaign browser; its run screens are superseded by the TUI.

EWKit: `EWUnit.py`, `EWWorker.py`, `build_worker_tarball.sh`, and the fixes listed above.

## Phases

### Phase 0: settle the unknowns

| Question | Done when |
|---|---|
| Can an AF job unpack the worker environment and complete an activity over mTLS through our ingress? | A test job finishes a no-op activity against the deployed frontend |
| Can a k8s pod submit to the AF schedd (IDTOKENs), and as which identity? | `condor_submit` from the ops pod lands a job in the AF pool |
| Robot certificate and Rucio quota on `MWT2_UC_LOCALGROUPDISK`; who owns the rules? | Rule created and deleted by the robot account |
| Do workers read replicas as local POSIX paths or through `root://`? Do they need a proxy? | A job reads one replicated file both ways, or we know which works |
| Can k8s pods read job outputs under `/data`? | Decides whether `verify_unit` runs in k8s or on a `verify` queue served from the AF |
| AF long-queue walltime and memory limits | Unit size and worker walltime target chosen |
| Worker-only authorization in open-source Temporal | Chosen design proven: a worker certificate cannot start or cancel workflows |

### Phase 1: platform

Temporal and Postgres on k8s, ingress, CA and certificates, namespace and search attributes,
backups, metrics. Done when the phase 0 test job runs against it.

### Phase 2: workflows against fakes

Workflows and activities with fake Rucio and HTCondor. Tests use Temporal's time-skipping test
environment for the timers (stall detection, rule lifetime extension) and replay tests against
saved histories on CI to catch determinism breaks. Done when a simulated production with
injected evictions, out-of-memory failures, stuck rules and a bad file finishes with the
invariants holding.

### Phase 3: EWKit worker

`EWUnit.py`, `EWWorker.py`, the tarball build, and the EWKit fixes, merged upstream. Done when a
worker on the AF runs real units from two datasets against the test server and the manifests
verify.

### Phase 4: first real production

Five datasets, `-e 1000`, real rules, real workers, manual decisions through a minimal CLI. Done
when the coverage and event-count invariants hold and the ntuple lists match what
`get_ntuples.py` would produce for the same outputs.

### Phase 5: agents and TUI

Selections in Postgres, approvals, the TUI, the triage agent with fixed-rule matching first,
batched triage, MLflow evaluation export. Done when a planted failure produces a proposal, a
person approves it in the TUI, and the fix is applied and recorded.

### Phase 6: scale and hardening

A full signal list (about 3.4k datasets): batched child workflows, continue-as-new, pool keeper
tuning, alerting, worker versioning drills, backup and restore test.

## Open questions and risks

- **Exposing the frontend to the internet** is the biggest new attack surface. The authorization
  design in phase 0 is a hard gate before any real credentials are deployed.
- **Quota against decision D11.** About 21k unique DIDs across all lists against a 15 TB group
  disk. Approval must show bytes against quota, rules must be short-lived, and XCache stays the
  fallback per dataset.
- **Workflow code changes during long productions** need versioning discipline from day one.
- **History size** for large productions: batching and continue-as-new must be in place before
  phase 6.
- **Exactly-once is not guaranteed.** Attempt-scoped outputs and verification are what prevent
  double-counted sum of weights; they are not optional.
- **Snapshot drift** for growing data containers needs a policy: freeze, or extend with approval.
- **Facility sign-off** for robot identity, accounting groups and remote submission.

## Why not Dagster

Dagster was evaluated first. With multi-dataset workers, week-long jobs and a ledger owning
units, leases and retries, Dagster would have been reduced to a sensor host and a status page,
while adding a second source of truth, an unauthenticated web UI to protect, and slow partition
views at around 21k datasets. Dagster Pipes needs the launching step to stay alive for the whole
job and has no way to reattach. Temporal instead provides the coordination pieces this design
needs: task queues that workers pull from, heartbeats and timeouts, retry policies, durable
timers and waits for human decisions, cancellation that cascades, and a full history for audit.
Dagster may still be useful later for the analysis steps after the ntuples.
