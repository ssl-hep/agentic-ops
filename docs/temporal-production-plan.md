# EWKit ntuple production on Temporal: architecture plan

Status: proposal. Written 2026-10-22. Phase 0 probes built 2026-10-22; see [phase0-results.md](phase0-results.md).
Revised 2026-10-23: replication rules and replica monitoring dropped; inputs are read in place,
only through the AF XCache, with no grid proxy on worker nodes.
Revised 2026-10-23: phase 0 findings folded in (endpoint, authorization, AF limits, unit
sizing); phase 1 planned. Phase 0 is done except the checks that need the robot identity.
Revised 2026-10-23: development runs with no robot, as Ben's personal identity, with data under
`/data/bengal1/agentic-ops`; see [Development scope](#development-scope). Users run the TUI on
an AF login node, and productions use the user's own EWKit clone and build.
Revised 2026-10-23: phase 1 deployment decided and built: our own chart (`charts/temporal`),
one server process, groundhog2k Postgres, the LoadBalancer IP as the endpoint until there is a
DNS record or passthrough, and the server image built by CI for every branch.

This plan describes how agentic-ops automates EWKit ntuple production end to end: an analyst
narrows a dataset list with the agent, approves it, HTCondor workers on the UChicago AF read
the DAODs where Rucio already has them and process them, failures get triaged with
agent-proposed fixes, and reliable bookkeeping shows exactly what work remains.

It replaces two earlier designs:

- the LangGraph orchestrator, Postgres checkpointer and HTTP ops-executor in
  `docs/agentic-ops-proposal.html` (EWKit v1.1 draft, on the `sampledata` branch)
- a Dagster design (assets partitioned by production × dataset, Dagster Pipes into HTCondor)
  that was considered and dropped; see [Why not Dagster](#why-not-dagster)

## Development scope

Decided 2026-10-23, for all of development (phases 1–5):

- **No robot identity.** Every credential is Ben's: his AF account's IDTOKEN for submission, his
  VOMS proxy for read-only Rucio lookups. Jobs run as `bengal1` with no accounting group, and
  there is one user. The robot (or per-user tokens) comes back before anyone else runs
  productions; nothing below depends on which it is, because the ops-executor holds whatever
  identity it is given.
- **Data root `/data/bengal1/agentic-ops`.** Builds, outputs, manifests, logs and lists go under
  this one configured root; nothing hard-codes it. `/data/bengal1` is a symlink to
  `/cold/user/bengal1`, so path checks resolve symlinks first (see
  [Work on `/data`](#work-on-data)). `/data/agentic-ops` is not requested from the facility
  until production.
- **The TUI runs on an AF login node**, as the user, and a production uses that user's EWKit
  clone and build; see [Code from the user's clone](#code-from-the-users-clone).

## Requirements

1. One HTCondor worker processes work from many datasets.
2. Worker code comes from the user's EWKit clone and build, packed into a tarball when the
   production is launched and recorded with its commit; not from an apptainer image.
3. Productions run for days to a week or more. Nothing may depend on a process staying alive
   for that long.
4. Failed work is re-run reliably, and at any moment we can say exactly what remains, per
   production and per dataset.
5. An agent proposes fixes for failures. People approve anything that changes physics or
   spends significant compute. Agents never hold grid credentials (decision D1 in
   `docs/procedural-ops-credentials.md` on the `mlflow` branch).
6. The Temporal server and its database run on Kubernetes (River). AF HTCondor workers reach
   gRPC endpoints on the internet, including port 7233 (verified in phase 0).
7. Kubernetes pods cannot read `/data`. Everything that touches files there (builds, outputs,
   manifests, logs) runs on the AF side; see [Work on `/data`](#work-on-data).
8. Inputs are read in place over `root://` from the replicas Rucio already has, only through
   the AF's XCache. Worker nodes hold no grid credential. agentic-ops creates no replication rules and watches no transfers; see
   [Reading inputs](#reading-inputs).

## Architecture overview

```
 analyst ── TUI (login node) ─HTTPS▶ backend (FastAPI) ──▶ Postgres: selections, approvals, reporting
                                       │  narrowing agent, triage agent (read-only tools)
                                       │  Temporal client: start production, send decisions (Updates)
                                       ▼
                          Temporal server (k8s) ◀── Postgres: temporal, temporal_visibility
                            ▲      ▲        ▲
              gRPC (mTLS)   │      │        │  gRPC (mTLS, public LoadBalancer :7233)
     ┌──────────────────────┘      │        └──────────────────────────────┐
     │ k8s worker pods             │                                       │ AF HTCondor jobs
     │  controller queue           │ ops-executor pod                      │  queue ewkit-<prod>-<mem>
     │   planning, reporting       │  queue ops                            │   run_unit: EWRun.py on a
     │  triage queue               │  x509 proxy + Rucio + condor creds    │   chunk of files, heartbeats
     │   LLM key, read-only tools  │  snapshot_dataset, refresh_replicas   │   inputs: root:// via XCache
     │                             │  (read-only Rucio), condor_submit/rm  │  queue af-io
     │                             │                                       │   check build, verify, logs,
     │                             │                                       │   cleanup on /data
     └─────────────────────────────┴───────────────────────────────────────┘
                                                                     /data (AF only, not visible to k8s)
```

The pieces:

| Component | Runs on | Role |
|---|---|---|
| Temporal server | k8s, our own server image (stock Temporal plus the agentic-ops authorizer) | Durable workflow state, task queues, timers, history, Web UI |
| Postgres | k8s | Temporal persistence and visibility; agentic-ops schema for selections, approvals, snapshots and reporting |
| backend | k8s, existing FastAPI app | Selection and agent APIs, approval API, Temporal client. The only place users act from |
| TUI | AF login node, as the user | Thin client of the backend: narrow, approve, watch, decide. At launch it also packs the user's EWKit build into the data root |
| controller workers | k8s pods | Workflows, plus activities that need no credentials and no files: planning, reporting |
| ops-executor | k8s pod with a credential sidecar | The only holder of the x509, Rucio and HTCondor credentials (Ben's during development, the robot's later). Polls `ops` only |
| triage workers | k8s pods | The triage agent: LLM key and read-only tools. Polls `triage` only |
| HTCondor workers | AF worker nodes | Poll `ewkit-<production>-<memclass>`, run units reading their inputs over `root://`, heartbeat, write outputs |
| af-io workers | AF worker nodes, small HTCondor jobs | Poll `af-io`: build EWKit, verify outputs, fetch logs, clean up, write lists. The only workers besides unit workers that touch `/data` |

## Core concepts

**Production.** One immutable combination of analysis folder, config name, EWKit clone path and
commit SHA (with a digest of any uncommitted changes), AnalysisBase release, build tarball
digest and output tag, plus the approved selection it runs over. Code is never changed
inside a production. A fix that needs new code means a new production, which can reuse finished
work only if a person explicitly approves the two as compatible.

**Selection.** The approved list of Rucio DIDs, kept in Postgres with a content hash, the user
who approved it and the totals they saw (bytes to read, files with no disk replica, estimated
core-hours). Workflows
receive a `selection_id`, never the list itself. A 3.4k-DID list is about 350 KB; Temporal's
default payload limit is 2 MB per payload and every payload is kept in history, so passing the
list around would bloat histories for no gain.

**Snapshot.** When a dataset is planned, every input file (GUID, LFN, size, checksum, event
count from Rucio) is frozen into `dataset_snapshots`, together with the file's disk replicas at
that moment. Coverage and event-count checks are made against the files. Replicas only say
where to read a file, and are refreshed when reading fails.

**Unit.** A chunk of files from one dataset, sized for 2–4 hours of EWRun, planned from
first-read input rates rather than cached ones. Units are what workers
process; one worker processes many units, from any datasets of its production. Large data
containers are split by files, never processed as a whole.

**Attempt.** One try at a unit. Output paths include the attempt number, so a late finisher can
never overwrite or double-count the attempt that was accepted.

## Workflows

Workflow IDs are deterministic, so starting a workflow that already exists is a no-op:
`production/<production_id>`, `dataset/<production_id>/<did>`, `pool-keeper/<production_id>`.
The authorizer relies on these: a unit worker may only use task tokens of `production/<id>`
and `dataset/<id>/…` workflows, so only those workflows may schedule activities on
`ewkit-<production>-<memclass>` queues. Production IDs are `[a-z0-9]{1,40}` (no `-`), so one
production's queues can never match another's.

### ProductionWorkflow

Input: `production_id`.

1. The production arrives with its build already packed by the TUI (see
   [Code from the user's clone](#code-from-the-users-clone)): tarball path and digest, commit,
   release. `check_build` on `af-io` confirms that a worker node can read the tarball and that
   its digest matches.
2. Smoke test: run one unit per campaign with `-e 1000`. Wait for the launch approval
   (an Update from the backend).
3. Start one child `DatasetWorkflow` per dataset, at most N running at once, in batches so the
   parent's history stays small. Use continue-as-new between batches.
4. Start or update `PoolKeeperWorkflow` for this production.
5. When every dataset is complete: build the ntuple lists (the equivalent of the
   `data/<A>/ntuples/*.txt` files `get_ntuples.py` produces today, but listing accepted output
   paths) on `controller` from the reporting tables, offer them for download through the
   backend, and write copies next to the outputs with `write_lists` on `af-io`. Then mark the
   production complete.

Handles: `cancel` (cascades to children and running activities, then `condor_rm` by production
tag), `pause`/`resume` (the pool keeper stops submitting), and a failure-batch signal from
datasets (see [Triage](#failure-handling-and-triage)).

### DatasetWorkflow

Input: `production_id`, DID.

1. `snapshot_dataset` on `ops`: read-only Rucio calls list the DID's files (GUID, size,
   checksum, events) and each file's disk replicas as `root://` PFNs. Files with no disk
   replica (only on tape, or lost) are not planned. The dataset then waits for a person to
   exclude them, or to have them staged outside agentic-ops and ask for a refresh.
2. Plan units over the snapshot at once: there is no transfer to wait for.
3. Run each unit (`run_unit` on `ewkit-<production>-<memclass>`), then `verify_unit` on
   `af-io`. When a unit fails because no replica of a file could be read, `refresh_replicas`
   on `ops` re-resolves those files before the retry.
4. Handle permanent failures through the triage loop.
5. When every unit is `DONE` or approved `EXCLUDED`: record the dataset as complete with its
   events, sum of weights, output list and exclusions, and tell the parent.

Search attributes: `ProductionId`, `Dataset`, `DatasetState`, `UnitsDone`, `UnitsTotal`,
`FilesWithoutReplica`, `NeedsDecision`.

### PoolKeeperWorkflow

One per production. Every few minutes it compares each task queue's backlog (Temporal's task
queue statistics) and running workers with targets, and calls `condor_submit` on `ops` for more
workers, with the matching memory request per `memclass` queue. It stops when the production is
paused, cancelled or done, and removes idle workers at the end. It also removes its own held
jobs: the AF only removes held or idle jobs after 30 days.

A separate `AfIoKeeperWorkflow` (one for the whole system) starts `af-io` workers on demand
when the `af-io` queue has a backlog, and adds more as it grows; idle `af-io` workers exit
after a few minutes. Phase 0 measured about 15 s to start a small job (worst 32 s in five
runs), so none are kept warm. One phase 0 job did wait 16 minutes, so schedule-to-start
latency on `af-io` is alerted on, and keeping one warm stays an option.

## The HTCondor worker

### What runs in the job

Each job is told the paths of two tarballs under the data root: the production's EWKit build,
`builds/<digest>/` (packed from the user's clone; see
[Code from the user's clone](#code-from-the-users-clone)), and a self-contained worker Python
environment, `envs/<digest>/` (python-build-standalone plus `temporalio` and `uproot`, built
for EL9 by agentic-ops and shared by productions). The ops pod submits jobs remotely and cannot read
`/data`, so the tarballs are not sent with `transfer_input_files`; the job reads them from
`/data` itself. A small bootstrap script, sent with the submission, does that. The job:

1. Copies both tarballs into `$_CONDOR_SCRATCH_DIR`, checks their digests against the ones
   recorded on the production, and unpacks them. A wrong digest stops the job. In phase 0 the
   worker environment (python-build-standalone 3.12, temporalio 1.34, uproot 5.7; 72 MB)
   copied from `/data` in under a second and unpacked in about 3 s.
2. Starts the worker shim in the bundled Python. The shim connects to Temporal with the
   production's client certificate and polls `ewkit-<production>-<memclass>` with one activity
   slot per requested core.
3. For each unit, runs EWRun in a subprocess under ALRB from cvmfs and the build's `setup.sh`:
   `EWRun.py --analysis A --configName C -i inputs.txt -o <unit>.a<n>.root --merge-output-files`.
4. Heartbeats every minute with events processed so far. A watchdog kills EWRun if the output
   stops growing or the unit exceeds a hard time limit.
5. Writes the output and `manifest/<unit>.a<n>.json` (temp file, then rename).
6. Stops taking new units after 24 hours, finishes the one it has, and exits cleanly. The pool
   keeper replaces it. That is far inside the AF's 7-day hold, so an eviction or node drain
   loses at most one unit, and worker certificates can be short-lived.

### Submitting to the AF

Settled in phase 0 (questions 2 and 7):

- The ops-executor submits remotely to the schedd on `head04.af.uchicago.edu` (collector
  `condor.af.uchicago.edu:9618`, HTCondor 25.0) with an IDTOKEN, and spools the bootstrap
  script and the job's certificate with the submission (phase 0 spooled a probe's input from
  a River pod; spooling the certificate is checked in phase 3). A spooled job is held with
  `HoldReasonCode` 16 until its input arrives; that is not a failure.
- HTCondor refuses a token file others can read, so the pod copies the mounted Secret to a
  private 0600 file before use.
- **A job runs as the AF user the token belongs to**, with that user's uid and group. During
  development that is `bengal1`, with no `AcctGroup`. Production use needs a token for the
  robot's own AF account (or per-user tokens), and the job then sets `accounting_group`.
- Every submission sets `request_memory`: the AF's default is the job's past usage. Limits
  enforced at submit: at most 16 CPUs and 128 GB per job. Jobs that run 7 days are held; there
  is no long queue to ask for. Jobs never set `Queue = "short"` (held after 4 hours).
- The job's working directory and event log must not be under `/data`, and nothing may be
  transferred from or to `/data` or `/work`. The bootstrap reads the tarballs from `/data`
  itself and the worker writes outputs there directly, which the AF allows.
- Memory classes are `4g`, `8g` and `16g` (EWKit's own default request is 2 GB), so the queues
  are `ewkit-<production>-4g` and so on. `af-io` jobs ask for one core and a couple of GB.
- AF HTCondor slots run as Kubernetes pods (`workers-ads-…`) on AlmaLinux 9, with `/data` and
  `/cvmfs/atlas.cern.ch` mounted and no X.509 proxy set.

### Code in EWKit

The worker code lives in EWKit, split so the core has no Temporal dependency:

| File | Purpose |
|---|---|
| `python/EWUnit.py` | `run_unit(spec) -> manifest`: runs EWRun on a unit spec and writes the manifest. No Temporal import |
| `share/EWWorker.py` | The Temporal shim: activity definition, heartbeats, walltime logic. Imports `temporalio` |
| `share/EWAfIo.py` | The `af-io` worker: check builds, verify, fetch logs, clean up, write lists. Imports `temporalio` and `uproot` |
| `share/pack_build.sh` | Packs a built clone into the build tarball: the InstallArea and the run-time files EWRun reads from the source tree. The TUI runs it |

The worker Python environment is not EWKit's: it is built by agentic-ops (from
`phase0/workerenv/build.sh`) and changes only when agentic-ops' worker dependencies do.

### Code from the user's clone

Users run the TUI on an AF login node, as themselves, so it can read their EWKit clone and
write to the data root. They build EWKit in the clone as they do today; agentic-ops does not
build it. When the user launches a production, the TUI:

1. Takes the clone's path (configured per user) and checks that it has been built.
2. Records the HEAD commit, branch, AnalysisBase release of the build, and `git status`.
   Uncommitted changes are allowed during development: the diff is saved next to the tarball,
   its digest goes on the production, and the approval screen shows it.
3. Runs `pack_build.sh` to write the build tarball to `<data root>/builds/<digest>/`, and sends
   its path, digest and the recorded metadata to the backend with the launch request.

Workers only ever see the tarball, never the clone. That keeps a production immutable while
the user goes on editing and rebuilding, and avoids depending on the clone being visible from
worker nodes (home directories may not be). A changed clone is a new production. Which
run-time files besides the InstallArea EWRun needs (analysis configs, `data/`) is pinned down
in phase 3.

Unit spec (JSON): unit id, attempt, production, analysis, config name, tag, DID, input files
(each a GUID with its candidate `root://` PFNs in order), output directory, events expected.

Manifest (JSON): unit id, attempt, status, exit code, events processed, sum of weights,
cutflow, output path, size, checksum, the replica read for each input, host, start and end
times, log path.

### Reading inputs

Workers read DAODs where they already are. Nothing is copied to MWT2 first, so there is no
quota to manage and no transfer to watch.

- Each input file in a unit spec carries its candidate PFNs, ordered by a preference list kept
  in configuration: MWT2 replicas first, then other US disk sites, then the rest. Tape
  replicas are never candidates.
- Every read goes through the AF XCache: `root://xcache.af.uchicago.edu:1094//<pfn>`. There
  is no direct read of the origin. XCache authenticates to the origin with its own
  credential, so jobs carry no VOMS proxy (phase 0 question 4).
- Before EWRun starts, the worker stats each file through XCache and takes the first
  candidate that answers. EWRun then reads the chosen URLs, and the manifest records which
  replica each file came from.
- xrootd timeouts are set in the job's environment, and the watchdog covers reads too: in
  phase 0 one XCache read hung for 480 s and then failed while the same file read fine before
  and after. A hung or failed read moves on to the next candidate rather than waiting.
- When no candidate of a file can be read, `run_unit` fails with a transient input error.
  Before the retry, the dataset workflow runs `refresh_replicas` on `ops` for those files,
  because replicas move and secondary copies get deleted.
- Rates measured in phase 0: about 0.9–1 GB/s per stream for files already in the cache, and
  the origin's rate on a first read (25–250 MB/s from Europe and Canada, a few MB/s at worst).
  Unit sizes are planned from first-read rates.
- The cache looks keyed by the Rucio path, not the origin: a file read through MWT2 was already
  cached when read through INFN or SARA. To be confirmed with the AF admins; if it holds, the
  candidate order matters only on a cache miss.

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
| `productions` | Analysis, config, clone path, SHA, uncommitted-diff digest, release, tarball path and digest, tag, selection, owner, state, approvals |
| `dataset_snapshots` | Per file: DID, GUID, LFN, size, checksum, events, snapshot time |
| `file_replicas` | Per file and replica: RSE, PFN, preference, resolved time, last read outcome. Replaced by `refresh_replicas` |
| `units` | Production, DID, file list, state, attempt count, failure count, memclass, parent unit (after a split) |
| `attempts` | Append-only: unit, attempt, worker, condor `cluster.proc`, host, times, outcome, error class, output path, manifest |
| `decisions` | Append-only: proposal, who decided, decision, outcome. Mirrors the Updates in workflow history |

Unit states: `PLANNED`, `QUEUED`, `RUNNING`, `VERIFYING`, `DONE`, `RETRY_WAIT`, `BLOCKED`,
`NEEDS_DECISION`, `EXCLUDED`, `SUPERSEDED`, `CANCELLED`.

### Verification

`verify_unit` runs on `af-io`, so it reads the output directly from `/data`, on a different
worker from the one that wrote it. It returns a small result (sizes, checksum, event counts, sum
of weights, cutflow); k8s never needs the file. It accepts an attempt only if:

- the output exists at its expected path and its size and checksum match the manifest
- the tree and the CutBookkeeper histograms are present (uproot; no ROOT needed)
- events processed equals the sum of Rucio event counts for the unit's input files
- sum of weights is above zero for MC

Accepting an attempt fixes it as the one that counts. Any later output for the same unit is
recorded as a duplicate and deleted by `cleanup_attempt` on `af-io`.

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
Files with no readable replica are counted on their own, because only a person can resolve them.

### Work on `/data`

Kubernetes pods cannot read `/data`, so the rule is: any activity that reads or writes files
there runs on the `af-io` queue, served by small HTCondor jobs on the AF. k8s only ever sees
what those activities return.

`/data` is CephFS (5.8 PB), mounted read-write in AF jobs; files are written as the job's user,
mode 644. Everything goes under one configured data root:

| Path under the root | Contents |
|---|---|
| `builds/<digest>/` | A production's build tarball, and the diff of uncommitted changes if any |
| `envs/<digest>/` | Worker Python environments |
| `productions/<production>/outputs/` | Unit outputs, `<unit>.a<n>.root` |
| `productions/<production>/manifests/` | Unit manifests |
| `productions/<production>/logs/` | Job and EWRun logs |
| `productions/<production>/lists/` | Ntuple lists |

During development the root is `/data/bengal1/agentic-ops`, which resolves to
`/cold/user/bengal1/agentic-ops` (ask the facility whether `/cold` is a slower tier). For
production use it moves to a shared `/data/agentic-ops`, which the facility has to create,
since `/data` is owned by root, with a group that both the submitting identity and the people
reading outputs belong to.

| Activity | Reads or writes | Returns |
|---|---|---|
| `check_build` | Reads the build tarball | Whether it is readable and its digest |
| `verify_unit` | Reads an output and its manifest | Sizes, checksum, events, sum of weights, cutflow |
| `fetch_log` | Reads a job log | An excerpt capped at 64 KB |
| `cleanup_attempt` | Deletes outputs of rejected or duplicate attempts | What was deleted |
| `write_lists` | Writes ntuple lists next to the outputs | Paths |

Rules for `af-io`:

- Results stay small. Temporal keeps every payload in history, and the default limit is 2 MB
  per payload, so logs and file listings are capped and summarised, never returned whole.
- Paths are checked against the production's output and build roots before any read, write or
  delete, after resolving symlinks on both sides (the development root `/data/bengal1` is a
  symlink into `/cold`). A path outside them fails the activity as non-retryable.
- `af-io` workers request one core and a couple of GB of memory. The queue is shared by all
  productions; the activities take everything they need as input.

## Failure handling and triage

### Retry policy

| Class | Examples | Handling |
|---|---|---|
| infrastructure | eviction, heartbeat timeout, node failure | Temporal retries automatically (heartbeat timeout 10 min) |
| transient input | xrootd timeout, no candidate replica readable | `refresh_replicas`, then retry with backoff; after N tries, `BLOCKED` |
| resources | out of memory, walltime | One retry on the next `memclass` queue, or propose a split |
| bad input | same file fails on two hosts | Non-retryable; proposal to exclude the file |
| code or config | traceback, same crash twice | Non-retryable; proposal for a new production |

`run_unit` declares `non_retryable_error_types` for the last two classes, so the agent only sees
failures Temporal's own retries could not fix.

### Triage loop

When a unit fails permanently, the dataset workflow:

1. runs `collect_evidence` on `controller`: log tail, exit code, host, hold reason, events
   processed (all attached to the failure by a worker interceptor), the replicas tried for each
   input and their errors, and similar failures elsewhere in the production. If the agent needs more of a log than the
   tail, `fetch_log` on `af-io` returns a bounded excerpt from `/data`
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

Input read failures use the same loop with different evidence (the replicas tried and their
errors, other replicas of the file, site downtimes) and actions: retry later, exclude the
files, or escalate to the site or DDM ops with a drafted ticket. agentic-ops never creates a
replication rule to fix one; staging a file is a person's action outside the system, followed
by a refresh.

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
| `ops` | ops-executor pod only | x509 proxy, Rucio account (read-only use, no attributes needed), IDTOKEN for the submitting AF account, the worker intermediate CA key. During development these are Ben's (below); later the robot's |
| `controller` | controller pods | Database role for the reporting schema |
| `triage` | triage pods | LLM key, MCP bearer token for read-only Rucio tools, MLflow |
| `ewkit-<production>-<memclass>` | HTCondor jobs | A short-lived client certificate for that production. No grid credential: inputs come through XCache |
| `af-io` | small HTCondor jobs | A short-lived client certificate for `af-io`; write access to the production output and build roots on `/data` |

The server is reachable from the internet, so:

- **mTLS on the frontend.** A dedicated CA; clients without a certificate, or with one from
  another CA, fail at the handshake. Subjects are `O=agentic-ops, OU=<role>, CN=<name>`.
  Roles: `admin` (system admin; never leaves the operator's machine), `controller` (namespace
  writer: controller pods, ops-executor, backend), `reader` (Web UI, the read-only Temporal MCP
  server; added in phase 1) and `worker`. Worker certificates are `CN=ewkit-<production>` or
  `CN=af-io`.
- **Worker certificates are short-lived.** The ops-executor signs one per submission from a
  worker-only intermediate CA, valid for a worker's 24 hours plus its last unit (about 30
  hours), and spools it with the job. It lives in the job's scratch directory, mode 0600,
  never on `/data` or another shared filesystem. The claim mapper accepts the `worker` role
  only from that intermediate and the other roles never from it, so a leaked intermediate key
  cannot mint a controller or admin.
- **Authorization (settled in phase 0).** Open-source Temporal's default authorizer needs the
  same writer role to poll a queue as to start, signal or cancel a workflow. Our server build
  adds a claim mapper and authorizer (`temporal-server/authz`): `admin` and `controller` go to
  the default authorizer; a `worker` may call only `GetSystemInfo`, `DescribeNamespace`,
  `PollActivityTaskQueue` on its own queues, `RecordActivityTaskHeartbeat`,
  `RespondActivityTask{Completed,Failed,Canceled}`, `ShutdownWorker` and
  `RecordWorkerHeartbeat`, and only with task tokens of its own production's workflows.
  Everything else is denied and logged (`Denied worker call`). Proven locally and on River.
  Residual risks are in [Open questions and risks](#open-questions-and-risks).
- **Only the frontend is exposed,** on port 7233 through a LoadBalancer Service, under a DNS
  name in the server certificate. Nothing may terminate TLS in front of it: that hides the
  client certificate the authorizer depends on. River's nginx ingress terminates TLS (no
  passthrough), so it cannot be used unless passthrough is enabled. The frontend's HTTP API
  stays off. The Web UI and admin tools stay behind CERN SSO (oauth2-proxy) on an internal
  address.
- **Personal credentials during development.** The ops-executor holds Ben's VOMS proxy and
  IDTOKEN as Secrets, and anyone who can read Secrets in the namespace can act as Ben on the AF
  and in Rucio. So: the shortest token lifetime that is workable (phase 0 used 1 day), the
  proxy uploaded and renewed by hand rather than his certificate and key stored in the cluster,
  and namespace access kept to the developers. With a robot, the sidecar renews the proxy from a
  long-lived Secret instead.
- **Users never talk to Temporal directly.** The TUI calls the backend, which checks the user's
  identity and the approvals table before starting workflows or sending Updates.
- **Accounting.** Jobs set `accounting_group` for the requesting user; phase 0 jobs submitted
  with a personal token had none. Needs facility sign-off.

## Deployment

- River, in namespace `ai-ops` where phase 0 ran, unless the River admins give agentic-ops its
  own. Our account there cannot list cluster-wide resources or read the ingress controller's
  settings; LoadBalancer Services get addresses, and `rook-ceph-block` volumes are available.
- Temporal server from our own image (`temporal-server/cmd/temporal-server`): stock Temporal
  1.32 built with the claim mapper and authorizer from `temporal-server/authz` and the Postgres
  plugin, plus `temporal-sql-tool` from the same module for schema setup. It replaces phase 0's
  one-process SQLite server. **Decided in phase 1:** our own chart, `charts/temporal`, running
  every service in one pod (the `temporalio/helm-charts` chart assumes the stock image's
  per-service command line); splitting services can wait for phase 6. The image is built and
  pushed to Docker Hub as `bengal1/agentic-ops-temporal-server:<branch>` on every push. Postgres
  databases `temporal` and `temporal_visibility` (SQL advanced visibility, so custom search
  attributes work), from the groundhog2k `postgres` subchart (official image; no bitnami
  charts or images), with a daily `pg_dump` CronJob. The agentic-ops schema lives on the same
  server.
- The frontend is published by a LoadBalancer Service on port 7233 with its own DNS record
  (see [Credentials and security](#credentials-and-security)). Until the record exists (or
  passthrough is enabled), clients dial the LoadBalancer IP and verify the certificate against
  the planned name: the chart publishes both, separately, in the `temporal-endpoint` ConfigMap
  (`TEMPORAL_ADDRESS`, `TEMPORAL_TLS_SERVER_NAME`, `TEMPORAL_NAMESPACE`), and every client and
  job takes them as separate settings, so the switch is a values change. The wildcard
  `*.servicex.ssl-hep.org` points at the ingress, so the record must be specific to the load
  balancer's address. gRPC health checks need a client certificate, so probes check the socket.
- Temporal namespace `ewkit-production`, retention long enough to cover a production plus
  review (30 days to start), with the search attributes above registered. The claim mapper
  scopes `controller`, `reader` and `worker` certificates to this one namespace.
- Worker Deployments (controller, ops-executor, triage) in the existing agentic-ops chart, each
  with its own ServiceAccount, NetworkPolicy and Secrets.
- Worker versioning, or `workflow.patched()` for every change to workflow code, because
  productions run for longer than the gap between deployments.
- Prometheus scraping of the server and SDK metrics; alerts on schedule-to-start latency,
  `NeedsDecision` older than 24 hours, the rate of input read failures, and server health.

## Repository changes

agentic-ops:

- `temporal-server/`: the Go module with the claim mapper and authorizer (from phase 0) and
  the production server command and image.
- `worker/`: a new Python package with workflows, controller activities, ops activities and
  triage activities. Separate entry points per task queue.
- `backend/`: selections move from the in-memory `SelectionStore` to Postgres; new approval and
  production endpoints; a Temporal client; the triage agent next to the narrower.
- `tui/`: a Textual app over the backend API, run on an AF login node: dataset table with
  facets, chat with the narrowing agent and a diff of its proposals, approval screen with
  totals and the build's commit and uncommitted changes, production progress, triage queue.
  The one thing it does besides calling the backend is pack the user's build at launch.
- `worker/env/`: the worker Python environment build, from `phase0/workerenv/`.
- `charts/agentic-ops/`: worker Deployments, ops-executor with the credential sidecar,
  Temporal as a dependency or a sibling release.
- Removed: the LangGraph run-setup stack on `sampledata`/`submit` is not carried forward. The
  Next.js frontend keeps the campaign browser; its run screens are superseded by the TUI.

EWKit: `EWUnit.py`, `EWWorker.py`, `EWAfIo.py`, `pack_build.sh`, and the fixes
listed above.

## Phases

### Phase 0: settle the unknowns

Done 2026-10-23, except the checks that need the robot identity. Probes and runbook:
[phase0/README.md](../phase0/README.md). Evidence: [phase0-results.md](phase0-results.md).
Every AF and Rucio probe ran with a personal account, which is also what development uses;
rows marked *robot* wait until the robot is requested, in phase 6.

| Question | Outcome | Still to do |
|---|---|---|
| Can an AF job unpack the worker environment and complete an activity over mTLS? | Yes, through a LoadBalancer Service on River. River's ingress terminates TLS and cannot carry mTLS | DNS name for the load balancer (phase 1) |
| Can a k8s pod submit to the AF schedd with IDTOKENs, and as which identity? | Yes, from River to `head04`. The job runs as the token's AF user | *Robot:* token for the robot's AF account; accounting group |
| Robot certificate and Rucio account; can the account list files and disk replicas? | A personal account with no attributes can | *Robot:* request it, then repeat |
| Can AF jobs read DAODs in place over `root://`? Credential, rate? | Through XCache with no proxy, from every site tried; direct reads need a proxy. **Decided: XCache only** | Facility: confirm cache keying and capacity at production scale |
| Do AF jobs mount `/data`, and as whom do they write? | Read-write, as the submitting user. Development uses `/data/bengal1/agentic-ops` | *Robot:* repeat in a shared `/data/agentic-ops` |
| How quickly does a small `af-io` job start? | About 15 s, worst 32 s in five runs; none kept warm. One other job waited 16 min | Ask the facility about the long wait |
| AF walltime and memory limits | 7-day hold, ≤16 CPUs and ≤128 GB per job. **Chosen:** units of 2–4 h, workers stop after 24 h, memory classes 4/8/16 GB | — |
| Worker-only authorization in open-source Temporal | Custom claim mapper and authorizer in the server binary; no separate frontend. Proven locally and on River | Repeat on the phase 1 endpoint |

### Phase 1: platform

The production Temporal server on River, reachable from AF jobs under a stable name, with the
agentic-ops authorizer, Postgres, the certificate scheme and basic operations. No workflows or
EWKit code yet.

Work:

1. **Server image.** Build Temporal 1.32 with `temporal-server/authz` and the Postgres plugin.
   *Built:* `temporal-server/cmd/temporal-server`, image from CI; own chart (see
   [Deployment](#deployment)).
2. **Postgres.** Databases `temporal` and `temporal_visibility` with their schemas, on
   `rook-ceph-block`; scheduled backups. *Built:* `charts/temporal`.
3. **Public endpoint.** LoadBalancer Service on 7233 and a DNS record for its address from the
   River admins, or SSL passthrough on the ingress if they prefer that. The server certificate
   carries that name. HTTP API off. *Until then:* the LoadBalancer IP, with the name and the
   address configured separately (`charts/temporal/README.md`).
4. **CA and certificates.** Root CA kept offline; server, `admin`, `controller` and `reader`
   certificates; a worker-only intermediate CA for the ops-executor; a small tool to sign
   worker certificates (about 30 hours). Changes to `authz`: the `reader` role, and roles
   checked against the issuing CA (worker only from the intermediate). Go tests and
   `test_authz.py` extended to cover them.
5. **Namespace.** `ewkit-production`, 30-day retention, the search attributes from
   [Workflows](#workflows). *Built:* `phase0/shim/admin.py --search-attributes`, run with the
   admin certificate.
6. **Web UI** behind oauth2-proxy with CERN SSO, on an internal address, using a `reader`
   certificate.
7. **Metrics.** Server metrics (port 9090) scraped by whatever Prometheus River offers (to find
   out), and a server health alert. The full alert set comes in phase 6.

Done when:

- `test_authz.py` passes through the public DNS name, and the server logs every denial with the
  certificate subject
- an AF job (`phase0/jobs/noop.sub`) completes the no-op activity through that name, with a
  worker certificate signed by the intermediate
- a workflow started before a restart of every server pod completes after it
- a Postgres backup is restored into a scratch database

Requests to make now, because later phases wait on them:

| Request | From | Needed by |
|---|---|---|
| DNS record for the load balancer (or SSL passthrough) | River admins | Phase 1 |
| XCache: cache keying, capacity at production scale; is `/cold` a slower tier; why a small job waited 16 min; can `af-io` get priority | AF admins | Phase 4, before phase 6 |

The robot identity, accounting groups and a shared `/data/agentic-ops` are not requested
during development; see phase 6.

### Phase 2: workflows against fakes

Workflows and activities with fake Rucio and HTCondor. Tests use Temporal's time-skipping test
environment for the timers (retry backoff, pool keeper intervals) and replay tests against
saved histories on CI to catch determinism breaks. Done when a simulated production with
injected evictions, out-of-memory failures, unreadable and deleted replicas, tape-only files
and a bad file finishes with the invariants holding.

### Phase 3: EWKit worker

`EWUnit.py`, `EWWorker.py`, `EWAfIo.py`, `pack_build.sh`, and the EWKit fixes, merged
upstream; the worker environment build moved to `worker/env/`. Until the TUI exists, a build
is packed by running `pack_build.sh` on a login node by hand. Done when a build packed from
Ben's clone passes `check_build`, a unit worker on the AF, submitted from the ops-executor with
its certificate spooled, runs real units from two datasets against the phase 1 server and
writes under `/data/bengal1/agentic-ops`, and `verify_unit` accepts them.

### Phase 4: first real production

Five datasets, `-e 1000`, inputs read in place, real workers, manual decisions through a minimal CLI. Done
when the coverage and event-count invariants hold and the ntuple lists match what
`get_ntuples.py` would produce for the same outputs.

### Phase 5: agents and TUI

Selections in Postgres, approvals, the TUI on a login node (including packing the user's
build at launch), the triage agent with fixed-rule matching first, batched triage, MLflow
evaluation export. Done when a production launched from the TUI with Ben's clone runs, a
planted failure produces a proposal, a person approves it in the TUI, and the fix is applied
and recorded.

### Phase 6: scale and hardening

A full signal list (about 3.4k datasets): batched child workflows, continue-as-new, pool keeper
tuning, alerting, worker versioning drills, backup and restore test.

Leaving development, before anyone but Ben runs productions:

- The submitting identity: a robot (x509 certificate, Rucio account, AF account and IDTOKEN on
  `head04`) or per-user tokens, so that jobs run as the user whose clone they use. Request it,
  then repeat phase 0 questions 2, 3 and 5 with it and record them in
  [phase0-results.md](phase0-results.md).
- Accounting groups, with facility sign-off.
- A shared data root, `/data/agentic-ops`, created by the facility, with a group shared by the
  submitting identity and the people reading outputs.
- More than one user: per-user clone paths and productions owned by their user.

## Open questions and risks

- **Exposing the frontend to the internet** is the biggest new attack surface. Phase 0 proved
  the worker-only authorization; phase 1 repeats it on the production endpoint before any real
  credentials are deployed. What it leaves open:
  - **Task tokens are not signed.** A unit worker's certificate is limited to its own
    production's workflows, but the `af-io` certificate serves every production, so a leaked
    one could complete or fail any `af-io` activity whose token it can build. Hence short
    lifetimes, and `af-io` certificates only ever go to `af-io` jobs.
  - `ShutdownWorker` and `RecordWorkerHeartbeat` are allowed without a queue check; at worst a
    worker disturbs sticky-queue cleanup or worker status in the namespace.
  - Anything that terminates TLS in front of the frontend hides the client certificate. The
    tests are repeated whenever the endpoint changes (new name, passthrough, a proxy), and if
    the HTTP API is ever turned on.
- **Reading over the WAN.** Every attempt reads its inputs across the network, which makes
  XCache, the fallback in decision D11, the only path. Throughput, XCache capacity and the
  load on remote sites limit how fast a production runs. A retried unit reads its files
  again, and XCache absorbs that only while they stay cached.
- **XCache is a single dependency.** With no direct fallback, an XCache outage stops every
  unit from reading. Units then fail as transient input errors and back off; the pool keeper
  pauses submission when the input-read failure rate spikes across datasets, rather than
  burning retries. XCache health is part of the triage evidence and alerting.
- **Nothing pins the inputs.** Without our own rules, secondary replicas can be deleted during
  a production, and files only on tape cannot be read in place. Planning reports tape-only
  files, `refresh_replicas` recovers moved ones, and staging stays a person's decision
  outside agentic-ops.
- **Workflow code changes during long productions** need versioning discipline from day one.
- **History size** for large productions: batching and continue-as-new must be in place before
  phase 6.
- **`af-io` depends on HTCondor scheduling.** If no `af-io` job can start (pool full, outage),
  build checks, verification and cleanup stop, even though unit workers keep running. Phase 0 saw
  starts of 12–32 s with the pool lightly loaded, but also one 16-minute wait. Alerting on the
  queue's schedule-to-start latency covers this; keeping one warm, or a dedicated slot or
  higher priority for `af-io` jobs if the facility allows one, would remove it.
- **Development runs on Ben's personal credentials.** Everything the system does on the AF and in
  Rucio is done as him, from Secrets in a shared namespace; see
  [Credentials and security](#credentials-and-security). Outputs under `/data/bengal1` are his
  files. The switch to a robot or per-user tokens, and to `/data/agentic-ops`, is phase 6 work
  that has not been tested; keeping the identity and data root as configuration is what keeps
  it small.
- **Code comes from a user's clone, not a reviewed release.** A production is reproducible
  only through its recorded commit, its uncommitted diff and the packed tarball, so the diff
  and tarball are kept as long as the production's outputs are.
- **Exactly-once is not guaranteed.** Attempt-scoped outputs and verification are what prevent
  double-counted sum of weights; they are not optional.
- **Snapshot drift** for growing data containers needs a policy: freeze, or extend with approval.
- **Facility sign-off** for remote submission and XCache use at production scale, and, when
  development ends, for the robot identity, accounting groups and `/data/agentic-ops`.

## Why not Dagster

Dagster was evaluated first. With multi-dataset workers, week-long jobs and a ledger owning
units, leases and retries, Dagster would have been reduced to a sensor host and a status page,
while adding a second source of truth, an unauthenticated web UI to protect, and slow partition
views at around 21k datasets. Dagster Pipes needs the launching step to stay alive for the whole
job and has no way to reattach. Temporal instead provides the coordination pieces this design
needs: task queues that workers pull from, heartbeats and timeouts, retry policies, durable
timers and waits for human decisions, cancellation that cascades, and a full history for audit.
Dagster may still be useful later for the analysis steps after the ntuples.
