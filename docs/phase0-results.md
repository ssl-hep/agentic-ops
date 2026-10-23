# Phase 0 results

Answers to the Phase 0 questions in [the production plan](temporal-production-plan.md#phase-0-settle-the-unknowns).
How to run each probe: [phase0/README.md](../phase0/README.md). Record the date, who ran it and
the evidence (a log excerpt, a command's output) with every answer.

No robot certificate or robot Rucio account exists yet. Every AF and Rucio probe runs with a
personal identity first; the answers say so, and need repeating with the robot once it exists.

## Summary

| # | Question | Status |
|---|---|---|
| 1 | AF job unpacks the worker environment and completes an activity over mTLS through our ingress | **Answered** for the personal account: an AF job completed the activity through a LoadBalancer Service (the ingress does not pass TLS through). Needs a DNS name for phase 1 |
| 2 | k8s pod submits to the AF schedd with IDTOKENs; which identity | **Answered** for the personal account: works from River, runs as the token's user. Robot token pending |
| 3 | Robot certificate and Rucio account; can it resolve replicas (read-only) | Pending; robot not requested yet |
| 4 | Reading DAODs in place over `root://`, MWT2 and remote, direct and via XCache; credential; rate | **Answered**: works through XCache without a proxy; direct reads need one. Decided: XCache only, no proxy on workers |
| 5 | `/data` in AF jobs; write identity | **Answered** for the personal account; `/data/agentic-ops` needs creating by the facility |
| 6 | Start latency of a small `af-io` job | **Answered**: about 15 s, worst 32 s; no need to keep workers warm |
| 7 | AF walltime and memory limits | **Answered**: 7-day hold, ≤16 CPUs, ≤128 GB. Unit size and walltime target proposed, to confirm |
| 8 | Worker-only authorization in open-source Temporal | **Answered**, locally and on River through the LoadBalancer Service |

## 8. Worker-only authorization

2026-10-22, local (OrbStack), Ben Galewsky with Claude Code.

**Answer: a custom claim mapper and authorizer built into the server binary. No separate
frontend endpoint is needed.** Code in `temporal-server/authz`, server version 1.32.1.

- The default authorizer cannot do it: every write API (poll, respond, start, signal, cancel,
  terminate) needs the same `RoleWriter`.
- `CallTarget.Request` carries the decoded request, so the authorizer can check the task queue
  of `PollActivityTaskQueue` and decode the task token of heartbeats and responses.
- Identities come from the client certificate: `O=agentic-ops, OU=<role>, CN=<name>`.
  `admin` and `controller` go to the default authorizer (system admin, namespace writer).
  `worker` gets an allowlist: `GetSystemInfo`, `DescribeNamespace`,
  `PollActivityTaskQueue` on its own queues, `RecordActivityTaskHeartbeat`,
  `RespondActivityTask{Completed,Failed,Canceled}`, `ShutdownWorker`,
  `RecordWorkerHeartbeat`. Everything else is denied and logged as `Denied worker call`.
- Temporal's internal frontend serves the system worker with its own internal claims, so it
  is not affected.

Evidence: `go test ./...` (43 cases over identity × API × queue × token) and
`phase0/tests/test_authz.py` against the running server, 8 passed. With the
`ewkit-phase0` worker certificate the server denied, and logged:

```
StartWorkflowExecution, SignalWorkflowExecution, RequestCancelWorkflowExecution,
TerminateWorkflowExecution, DescribeWorkflowExecution, CountWorkflowExecutions,
GetWorkflowExecutionHistory, PollWorkflowTaskQueue, RespondActivityTaskCompletedById,
ListSearchAttributes (operator service)
PollActivityTaskQueue on ewkit-phase1-default and on af-io
af-io certificate: PollActivityTaskQueue on ewkit-phase0-default
ewkit-phase1 certificate: RespondActivityTaskCompleted with a token phase0's worker received
```

The same worker certificate polled its own queues and completed the no-op activity, and the
SDK's worker made no call that was denied. A client without a certificate, and one signed by
another CA, fail at the TLS handshake; a controller cannot register namespaces.

River, 2026-10-23, Ben Galewsky with Claude Code: the same tests through the LoadBalancer
Service (192.170.236.182:7233, certificate checked against
`temporal-phase0.servicex.ssl-hep.org`; see question 1). `test_authz.py`: 8 passed in
14.44 s. The server logged every denial from the local run, each once, with the full
certificate subject, so the client certificate reaches the frontend through the load
balancer:

```
CN=ewkit-phase0  StartWorkflowExecution, SignalWorkflowExecution,
                 RequestCancelWorkflowExecution, TerminateWorkflowExecution,
                 DescribeWorkflowExecution, CountWorkflowExecutions,
                 GetWorkflowExecutionHistory, PollWorkflowTaskQueue,
                 RespondActivityTaskCompletedById    workers may not call <API>
CN=ewkit-phase0  ListSearchAttributes (operator)     workers may only call the workflow service
CN=ewkit-phase0  PollActivityTaskQueue               this worker may not poll task queue "af-io"
CN=ewkit-phase0  PollActivityTaskQueue               ... "ewkit-phase1-default"
CN=af-io         PollActivityTaskQueue               ... "ewkit-phase0-default"
CN=ewkit-phase1  RespondActivityTaskCompleted        task token belongs to another production
```

Residual risks, to carry into phase 1:

- **Task tokens are not signed.** A unit worker's certificate is limited to tokens of its own
  production's workflows (`production/<id>`, `dataset/<id>/…`; production IDs are
  `[a-z0-9]+`). The `af-io` certificate serves every production, so a leaked `af-io`
  certificate could complete or fail any `af-io` activity whose token it can build. It needs a
  short lifetime and should only reach af-io jobs.
- `ShutdownWorker` and `RecordWorkerHeartbeat` are allowed without a queue check. At worst a
  worker can disturb sticky-queue cleanup or worker status for the namespace.
- The frontend HTTP API is off (no `httpPort`). If it is turned on, it needs the same test.
- On River it was run through the LoadBalancer Service only. It needs repeating if the
  endpoint moves to ingress passthrough or a new DNS name: anything that terminates TLS in
  front of the server hides the client certificate (see question 1).

## 1. Worker environment and a no-op activity over mTLS

Local part, 2026-10-22:

- `workerenv/build.sh` builds in `almalinux:9` (linux/amd64): python-build-standalone 3.12.15,
  temporalio 1.34.0, uproot 5.7.7 and the shim. 81 MB compressed.
- `jobs/bootstrap.sh` in a clean `almalinux:9` container with no network: copy 72 MB from
  the mounted "/data" in 0.1 s, digest check, unpack in 2.0 s. A wrong digest stops the job.
- The same container, as a non-root uid, served `probe_env` for a `NoopWorkflow` started on
  the host with the controller certificate. Schedule-to-start 1.2 s.

River, 2026-10-22: **the River nginx ingress does not pass TLS through.** The server is
deployed in `ai-ops` behind `temporal-phase0.servicex.ssl-hep.org`. The host resolves to the
ingress (192.170.236.250, same as MLflow), and the Ingress is admitted (class `nginx`,
address set, `Sync` events). Port 443 still serves nginx's default "Kubernetes Ingress
Controller Fake Certificate" (issuer `O=Acme Co`), so nginx terminates TLS itself. The
controller most likely runs without `--enable-ssl-passthrough`. Our account cannot read the
controller's arguments, and cannot list services cluster-wide to see whether River offers
LoadBalancer addresses.

**A LoadBalancer Service in `ai-ops` does get an address** (`deploy/service-lb.yaml`:
192.170.236.182, port 7233), and it serves the server's own certificate from our CA, so mTLS
reaches the frontend. The wildcard `*.servicex.ssl-hep.org` points at the ingress, so phase 0
clients connect to the IP and verify the certificate against
`temporal-phase0.servicex.ssl-hep.org`. Phase 1 needs a DNS record for the load balancer, or
passthrough enabled by the River admins.

AF, 2026-10-23, Ben Galewsky with Claude Code: `jobs/noop.sub` (cluster 4017461) with the
`ewkit-phase0` certificate, against 192.170.236.182:7233; the workflow was started with the
controller certificate.

**Answer: an AF job unpacks the worker environment and completes an activity over mTLS
through the load balancer. AF worker nodes reach port 7233 outbound.**

- The job ran on `c114` (private address 172.16.12.174, inside the slot pod
  `workers-ads-5f8df59d6-tnjl5`), AlmaLinux 9.8, uid 13179 (`bengal1`). Bundled Python
  3.12.15, temporalio 1.34.0, uproot 5.7.7. `/data` and `/cvmfs/atlas.cern.ch` are visible;
  no X.509 proxy is set.
- `bootstrap.sh`: copied 72 MB from `/data` in 0.9 s, unpacked by 3.2 s. The activity started
  4 s after the job did, sent 6 heartbeats and completed after 30 s. The worker stopped after
  one task, the job exited 0, and stderr is empty.
- Schedule-to-start was 1021.8 s, but almost all of it was the job waiting in the queue: the
  workflow was scheduled at 13:25:39 UTC, the job submitted at 13:26:10 and started at 13:42:35
  (16 min 25 s idle). That is far longer than any start in question 6, for a smaller job (1
  CPU, 2 GB). The cause is not known from this one job.

## 2. Submitting from a pod

The probe (`probes/condor_remote/`) ran against an `htcondor/mini` pool (HTCondor 25.13.2)
with the Python bindings 25.14.1 and an IDTOKEN: the WRITE ping reports the identity the
schedd maps the token to (`MyRemoteUserName`), the spooled job ran, its output came back, and
the job ad shows `Owner`, `User` and `AuthTokenSubject`. Two things found on the way:

- HTCondor refuses a token file readable by others, so the Job copies the mounted Secret to a
  private 0600 file before use.
- A spooled job is held for "Spooling input data files" (`HoldReasonCode` 16) until its input
  arrives; that hold is not a failure.

AF pool, from `probes/af_limits.sh` on login03 (2026-10-22): collector
`condor.af.uchicago.edu:9618` ("UChicago ATLAS Analysis Facility@head04"), HTCondor 25.0.14.
There are schedds on `head04` and `login01`–`04`; the login nodes submit to
`head04.af.uchicago.edu` (`SCHEDD_HOST`). From a login node, a WRITE ping to `head04` maps
to `bengal1@af.uchicago.edu`.

River → AF, 2026-10-23, Job `phase0-condor-probe` in `ai-ops`, with a token from
`condor_token_fetch` for the personal account:

**Answer: yes, a River pod can submit to the AF schedd with an IDTOKEN, and the job runs as
the token's identity.**

- The pod reached `head04.af.uchicago.edu` on port 9618. The WRITE ping succeeded over
  `IDTOKENS`, encrypted, and mapped the pod to `bengal1@af.uchicago.edu`. The token was issued
  by `head01.af.uchicago.edu`.
- The spooled job (cluster 4015936) started 21 s after submission on `c080`, exited 0, and its
  output was retrieved 9 s after it finished. Job ad: `Owner=bengal1`,
  `AuthTokenSubject=bengal1@af.uchicago.edu`, no `AcctGroup`.
- A second run (cluster 4017885, same token) gave the same ping and job ad and started 21 s
  after submission (19:05:10 to 19:05:31 UTC) on `c080`. On the worker the job runs as
  `uid=13179(bengal1) gid=5741(atlas-af)`, the personal account's own uid and primary group
  (the same uid as the question 1 job): the token maps to an ordinary local user.
  `/data/agentic-ops` does not exist (question 5) and `X509_USER_PROXY` is empty (question 4).
- A job therefore runs as whichever AF user the token belongs to. For production, that means
  a token for the robot's AF identity, issued by the facility, and an `AcctGroup` set by the
  executor (the plan's accounting item).

Pending: the same test with a robot token once the facility issues one. It should show the
robot's AF account in `MyRemoteUserName`, `AuthTokenSubject` and the `id` line, and whether
`AcctGroup` is set.

## 3. Robot certificate and Rucio account

Revised 2026-10-23: agentic-ops no longer creates replication rules (see the plan's
[Reading inputs](temporal-production-plan.md#reading-inputs)). The question is now only who
requests and owns the robot certificate and Rucio account, and whether the account can list a
DID's files and disk replicas. `probes/rucio_replicas.py` is read-only.

Pending. The robot has not been requested yet. Before the change, the probe found that
personal accounts have no quota on `MWT2_UC_LOCALGROUPDISK`; that no longer matters.

Personal account, 2026-10-23, `probes/rucio_replicas.py whoami` on the AF:

```
account: bgalewsk  status: ACTIVE  attributes: none
```

The account has no attributes (no admin or special roles), which is all a read-only lookup
needs. It also listed files and disk replicas for question 4's `pfns` run. The same two
checks are to be repeated with the robot account.

## 4. Reading in place over `root://`

2026-10-23, personal account, jobs 4014562 (first run) and 4015421 (second run) on AF
workers, `jobs/replica_read.sub`. Dataset `mc20_13TeV:…700320…DAOD_PHYSLITE…tid49685369_00`:
559 files of about 1.1–1.2 GB, **all with a disk replica, none only on tape**. Replicas per
site: FZK-LCG2 559, MWT2 250, TRIUMF 59, SARA-MATRIX 20, INFN-T1 20.

**Answer: yes, through the AF XCache, and without a proxy.**

- **Through XCache, reads work with and without the job's proxy, from every site.** XCache
  authenticates to the origin with its own credential.
- **Direct reads need a proxy at every site.** Without one they fail in 0.06–9 s.
- **Rates in MB/s** (bytes over wall time for `xrdcp` to `/dev/null`):

  | Site | Direct, with proxy | XCache, first read | XCache, cached |
  |---|---|---|---|
  | MWT2 | 316–891 | 697–827 | 909–967 |
  | FZK-LCG2 | 25–73 | 3 and 92 | 905–917 |
  | TRIUMF | 228–250 | 213–253 | 763–953 |
  | INFN-T1 | 90–107 | (already cached) 745–965 | 884–965 |
  | SARA-MATRIX | 96–125 | (already cached) 649–959 | 713–951 |

  INFN and SARA hold the same two files as MWT2 (`_000279`, `_000280`), and their first
  XCache reads were already at cached speed. So the cache looks keyed by the Rucio path, not by
  the origin. Ask the AF admins to confirm.
- **Cached reads run at about 0.9–1 GB/s per stream**, so a cached 1.1 GB file takes about
  1.2 s. First reads go at the origin's rate: FZK, the only site with every file of this
  dataset, gave 3 and 92 MB/s (one file took 126 s).
- **One read hung:** in the first run, an XCache read of MWT2 `_000279` without a proxy ran
  480 s and failed; the same file read fine before and after. Unit workers need xrootd
  timeouts and should retry the next path, not wait.

What this means for the plan:

- **Decided 2026-10-23: inputs are read only through XCache.** There is no direct fallback,
  so unit workers carry no grid proxy; the robot's proxy stays in the ops-executor. The
  price is that XCache is a single dependency; the plan's risks section covers it.
- Size units from first-read rates, not cached ones: the far sites read at 25–250 MB/s.

<details><summary>Raw results: cluster, path, proxy, outcome, seconds, MB/s, site, URL</summary>

```
4014562  direct proxy ok 3.513 316 MWT2_DATADISK root://fax.mwt2.org:1094//atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4014562  xcache proxy ok 1.596 697 MWT2_DATADISK root://xcache.af.uchicago.edu:1094//root://fax.mwt2.org:1094//atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4014562  direct noproxy FAIL 0.057 - MWT2_DATADISK root://fax.mwt2.org:1094//atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4014562  xcache noproxy ok 2.599 428 MWT2_DATADISK root://xcache.af.uchicago.edu:1094//root://fax.mwt2.org:1094//atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4014562  direct proxy ok 1.811 672 MWT2_DATADISK root://fax.mwt2.org:1094//atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4014562  xcache proxy ok 1.472 827 MWT2_DATADISK root://xcache.af.uchicago.edu:1094//root://fax.mwt2.org:1094//atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4014562  direct noproxy FAIL 0.065 - MWT2_DATADISK root://fax.mwt2.org:1094//atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4014562  xcache noproxy FAIL 480.102 - MWT2_DATADISK root://xcache.af.uchicago.edu:1094//root://fax.mwt2.org:1094//atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4014562  direct proxy ok 16.132 25 FZK-LCG2_DATADISK root://atlasdcache-kit.gridka.de:1094//pnfs/gridka.de/atlas/disk-only/atlasdatadisk/rucio/mc20_13TeV/83/b9/DAOD_PHYSLITE.49685369._000002.pool.root.1
4014562  xcache proxy ok 125.912 3 FZK-LCG2_DATADISK root://xcache.af.uchicago.edu:1094//root://atlasdcache-kit.gridka.de:1094//pnfs/gridka.de/atlas/disk-only/atlasdatadisk/rucio/mc20_13TeV/83/b9/DAOD_PHYSLITE.49685369._000002.pool.root.1
4014562  direct noproxy FAIL 0.822 - FZK-LCG2_DATADISK root://atlasdcache-kit.gridka.de:1094//pnfs/gridka.de/atlas/disk-only/atlasdatadisk/rucio/mc20_13TeV/83/b9/DAOD_PHYSLITE.49685369._000002.pool.root.1
4014562  xcache noproxy ok 0.713 573 FZK-LCG2_DATADISK root://xcache.af.uchicago.edu:1094//root://atlasdcache-kit.gridka.de:1094//pnfs/gridka.de/atlas/disk-only/atlasdatadisk/rucio/mc20_13TeV/83/b9/DAOD_PHYSLITE.49685369._000002.pool.root.1
4014562  direct proxy ok 10.889 37 FZK-LCG2_DATADISK root://atlasdcache-kit.gridka.de:1094//pnfs/gridka.de/atlas/disk-only/atlasdatadisk/rucio/mc20_13TeV/f0/ac/DAOD_PHYSLITE.49685369._000001.pool.root.1
4014562  xcache proxy ok 4.448 92 FZK-LCG2_DATADISK root://xcache.af.uchicago.edu:1094//root://atlasdcache-kit.gridka.de:1094//pnfs/gridka.de/atlas/disk-only/atlasdatadisk/rucio/mc20_13TeV/f0/ac/DAOD_PHYSLITE.49685369._000001.pool.root.1
4014562  direct noproxy FAIL 0.684 - FZK-LCG2_DATADISK root://atlasdcache-kit.gridka.de:1094//pnfs/gridka.de/atlas/disk-only/atlasdatadisk/rucio/mc20_13TeV/f0/ac/DAOD_PHYSLITE.49685369._000001.pool.root.1
4014562  xcache noproxy ok 0.491 835 FZK-LCG2_DATADISK root://xcache.af.uchicago.edu:1094//root://atlasdcache-kit.gridka.de:1094//pnfs/gridka.de/atlas/disk-only/atlasdatadisk/rucio/mc20_13TeV/f0/ac/DAOD_PHYSLITE.49685369._000001.pool.root.1
4014562  direct proxy ok 12.232 90 INFN-T1_DATADISK root://xrootd-atlas.cr.cnaf.infn.it:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4014562  xcache proxy ok 1.493 745 INFN-T1_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd-atlas.cr.cnaf.infn.it:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4014562  direct noproxy FAIL 3.202 - INFN-T1_DATADISK root://xrootd-atlas.cr.cnaf.infn.it:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4014562  xcache noproxy ok 1.207 922 INFN-T1_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd-atlas.cr.cnaf.infn.it:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4014562  direct proxy ok 11.329 107 INFN-T1_DATADISK root://xrootd-atlas.cr.cnaf.infn.it:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4014562  xcache proxy ok 1.377 884 INFN-T1_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd-atlas.cr.cnaf.infn.it:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4014562  direct noproxy FAIL 3.380 - INFN-T1_DATADISK root://xrootd-atlas.cr.cnaf.infn.it:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4014562  xcache noproxy ok 1.378 884 INFN-T1_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd-atlas.cr.cnaf.infn.it:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4014562  direct proxy ok 11.533 96 SARA-MATRIX_DATADISK root://xrootd.grid.sara.nl:1094//pnfs/grid.sara.nl/data/atlas/atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4014562  xcache proxy ok 1.160 959 SARA-MATRIX_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd.grid.sara.nl:1094//pnfs/grid.sara.nl/data/atlas/atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4014562  direct noproxy FAIL 3.575 - SARA-MATRIX_DATADISK root://xrootd.grid.sara.nl:1094//pnfs/grid.sara.nl/data/atlas/atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4014562  xcache noproxy ok 1.452 766 SARA-MATRIX_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd.grid.sara.nl:1094//pnfs/grid.sara.nl/data/atlas/atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4014562  direct proxy ok 9.858 123 SARA-MATRIX_DATADISK root://xrootd.grid.sara.nl:1094//pnfs/grid.sara.nl/data/atlas/atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4014562  xcache proxy ok 1.708 713 SARA-MATRIX_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd.grid.sara.nl:1094//pnfs/grid.sara.nl/data/atlas/atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4014562  direct noproxy FAIL 3.370 - SARA-MATRIX_DATADISK root://xrootd.grid.sara.nl:1094//pnfs/grid.sara.nl/data/atlas/atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4014562  xcache noproxy ok 1.876 649 SARA-MATRIX_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd.grid.sara.nl:1094//pnfs/grid.sara.nl/data/atlas/atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4014562  direct proxy ok 3.567 228 TRIUMF-LCG2_DATADISK root://xrootd.lcg.triumf.ca:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/85/3f/DAOD_PHYSLITE.49685369._000547.pool.root.1
4014562  xcache proxy ok 3.815 213 TRIUMF-LCG2_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd.lcg.triumf.ca:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/85/3f/DAOD_PHYSLITE.49685369._000547.pool.root.1
4014562  direct noproxy FAIL 8.943 - TRIUMF-LCG2_DATADISK root://xrootd.lcg.triumf.ca:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/85/3f/DAOD_PHYSLITE.49685369._000547.pool.root.1
4014562  xcache noproxy ok 0.872 935 TRIUMF-LCG2_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd.lcg.triumf.ca:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/85/3f/DAOD_PHYSLITE.49685369._000547.pool.root.1
4014562  direct proxy ok 3.360 243 TRIUMF-LCG2_DATADISK root://xrootd.lcg.triumf.ca:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/fc/08/DAOD_PHYSLITE.49685369._000546.pool.root.1
4014562  xcache proxy ok 3.226 253 TRIUMF-LCG2_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd.lcg.triumf.ca:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/fc/08/DAOD_PHYSLITE.49685369._000546.pool.root.1
4014562  direct noproxy FAIL 8.965 - TRIUMF-LCG2_DATADISK root://xrootd.lcg.triumf.ca:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/fc/08/DAOD_PHYSLITE.49685369._000546.pool.root.1
4014562  xcache noproxy ok 1.921 425 TRIUMF-LCG2_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd.lcg.triumf.ca:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/fc/08/DAOD_PHYSLITE.49685369._000546.pool.root.1
4015421  direct proxy ok 1.248 891 MWT2_DATADISK root://fax.mwt2.org:1094//atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4015421  xcache proxy ok 1.223 909 MWT2_DATADISK root://xcache.af.uchicago.edu:1094//root://fax.mwt2.org:1094//atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4015421  direct noproxy FAIL 0.068 - MWT2_DATADISK root://fax.mwt2.org:1094//atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4015421  xcache noproxy ok 1.150 967 MWT2_DATADISK root://xcache.af.uchicago.edu:1094//root://fax.mwt2.org:1094//atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4015421  direct proxy ok 2.177 559 MWT2_DATADISK root://fax.mwt2.org:1094//atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4015421  xcache proxy ok 1.330 916 MWT2_DATADISK root://xcache.af.uchicago.edu:1094//root://fax.mwt2.org:1094//atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4015421  direct noproxy FAIL 0.056 - MWT2_DATADISK root://fax.mwt2.org:1094//atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4015421  xcache noproxy ok 1.270 959 MWT2_DATADISK root://xcache.af.uchicago.edu:1094//root://fax.mwt2.org:1094//atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4015421  direct proxy ok 12.750 32 FZK-LCG2_DATADISK root://atlasdcache-kit.gridka.de:1094//pnfs/gridka.de/atlas/disk-only/atlasdatadisk/rucio/mc20_13TeV/83/b9/DAOD_PHYSLITE.49685369._000002.pool.root.1
4015421  xcache proxy ok 0.446 917 FZK-LCG2_DATADISK root://xcache.af.uchicago.edu:1094//root://atlasdcache-kit.gridka.de:1094//pnfs/gridka.de/atlas/disk-only/atlasdatadisk/rucio/mc20_13TeV/83/b9/DAOD_PHYSLITE.49685369._000002.pool.root.1
4015421  direct noproxy FAIL 0.661 - FZK-LCG2_DATADISK root://atlasdcache-kit.gridka.de:1094//pnfs/gridka.de/atlas/disk-only/atlasdatadisk/rucio/mc20_13TeV/83/b9/DAOD_PHYSLITE.49685369._000002.pool.root.1
4015421  xcache noproxy ok 0.449 911 FZK-LCG2_DATADISK root://xcache.af.uchicago.edu:1094//root://atlasdcache-kit.gridka.de:1094//pnfs/gridka.de/atlas/disk-only/atlasdatadisk/rucio/mc20_13TeV/83/b9/DAOD_PHYSLITE.49685369._000002.pool.root.1
4015421  direct proxy ok 5.615 73 FZK-LCG2_DATADISK root://atlasdcache-kit.gridka.de:1094//pnfs/gridka.de/atlas/disk-only/atlasdatadisk/rucio/mc20_13TeV/f0/ac/DAOD_PHYSLITE.49685369._000001.pool.root.1
4015421  xcache proxy ok 0.453 905 FZK-LCG2_DATADISK root://xcache.af.uchicago.edu:1094//root://atlasdcache-kit.gridka.de:1094//pnfs/gridka.de/atlas/disk-only/atlasdatadisk/rucio/mc20_13TeV/f0/ac/DAOD_PHYSLITE.49685369._000001.pool.root.1
4015421  direct noproxy FAIL 0.717 - FZK-LCG2_DATADISK root://atlasdcache-kit.gridka.de:1094//pnfs/gridka.de/atlas/disk-only/atlasdatadisk/rucio/mc20_13TeV/f0/ac/DAOD_PHYSLITE.49685369._000001.pool.root.1
4015421  xcache noproxy ok 0.452 907 FZK-LCG2_DATADISK root://xcache.af.uchicago.edu:1094//root://atlasdcache-kit.gridka.de:1094//pnfs/gridka.de/atlas/disk-only/atlasdatadisk/rucio/mc20_13TeV/f0/ac/DAOD_PHYSLITE.49685369._000001.pool.root.1
4015421  direct proxy ok 11.052 100 INFN-T1_DATADISK root://xrootd-atlas.cr.cnaf.infn.it:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4015421  xcache proxy ok 1.160 959 INFN-T1_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd-atlas.cr.cnaf.infn.it:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4015421  direct noproxy FAIL 3.221 - INFN-T1_DATADISK root://xrootd-atlas.cr.cnaf.infn.it:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4015421  xcache noproxy ok 1.153 965 INFN-T1_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd-atlas.cr.cnaf.infn.it:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4015421  direct proxy ok 11.554 105 INFN-T1_DATADISK root://xrootd-atlas.cr.cnaf.infn.it:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4015421  xcache proxy ok 1.293 942 INFN-T1_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd-atlas.cr.cnaf.infn.it:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4015421  direct noproxy FAIL 3.142 - INFN-T1_DATADISK root://xrootd-atlas.cr.cnaf.infn.it:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4015421  xcache noproxy ok 1.289 945 INFN-T1_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd-atlas.cr.cnaf.infn.it:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4015421  direct proxy ok 8.876 125 SARA-MATRIX_DATADISK root://xrootd.grid.sara.nl:1094//pnfs/grid.sara.nl/data/atlas/atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4015421  xcache proxy ok 1.169 951 SARA-MATRIX_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd.grid.sara.nl:1094//pnfs/grid.sara.nl/data/atlas/atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4015421  direct noproxy FAIL 2.792 - SARA-MATRIX_DATADISK root://xrootd.grid.sara.nl:1094//pnfs/grid.sara.nl/data/atlas/atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4015421  xcache noproxy ok 1.445 770 SARA-MATRIX_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd.grid.sara.nl:1094//pnfs/grid.sara.nl/data/atlas/atlasdatadisk/rucio/mc20_13TeV/f2/f2/DAOD_PHYSLITE.49685369._000280.pool.root.1
4015421  direct proxy ok 10.402 117 SARA-MATRIX_DATADISK root://xrootd.grid.sara.nl:1094//pnfs/grid.sara.nl/data/atlas/atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4015421  xcache proxy ok 1.559 781 SARA-MATRIX_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd.grid.sara.nl:1094//pnfs/grid.sara.nl/data/atlas/atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4015421  direct noproxy FAIL 2.697 - SARA-MATRIX_DATADISK root://xrootd.grid.sara.nl:1094//pnfs/grid.sara.nl/data/atlas/atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4015421  xcache noproxy ok 1.544 789 SARA-MATRIX_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd.grid.sara.nl:1094//pnfs/grid.sara.nl/data/atlas/atlasdatadisk/rucio/mc20_13TeV/2f/ea/DAOD_PHYSLITE.49685369._000279.pool.root.1
4015421  direct proxy ok 3.308 246 TRIUMF-LCG2_DATADISK root://xrootd.lcg.triumf.ca:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/85/3f/DAOD_PHYSLITE.49685369._000547.pool.root.1
4015421  xcache proxy ok 0.855 953 TRIUMF-LCG2_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd.lcg.triumf.ca:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/85/3f/DAOD_PHYSLITE.49685369._000547.pool.root.1
4015421  direct noproxy FAIL 8.783 - TRIUMF-LCG2_DATADISK root://xrootd.lcg.triumf.ca:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/85/3f/DAOD_PHYSLITE.49685369._000547.pool.root.1
4015421  xcache noproxy ok 0.864 943 TRIUMF-LCG2_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd.lcg.triumf.ca:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/85/3f/DAOD_PHYSLITE.49685369._000547.pool.root.1
4015421  direct proxy ok 3.259 250 TRIUMF-LCG2_DATADISK root://xrootd.lcg.triumf.ca:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/fc/08/DAOD_PHYSLITE.49685369._000546.pool.root.1
4015421  xcache proxy ok 0.869 939 TRIUMF-LCG2_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd.lcg.triumf.ca:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/fc/08/DAOD_PHYSLITE.49685369._000546.pool.root.1
4015421  direct noproxy FAIL 8.807 - TRIUMF-LCG2_DATADISK root://xrootd.lcg.triumf.ca:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/fc/08/DAOD_PHYSLITE.49685369._000546.pool.root.1
4015421  xcache noproxy ok 1.070 763 TRIUMF-LCG2_DATADISK root://xcache.af.uchicago.edu:1094//root://xrootd.lcg.triumf.ca:1094//atlas/atlasdatadisk/rucio/mc20_13TeV/fc/08/DAOD_PHYSLITE.49685369._000546.pool.root.1
```

</details>

## 5. `/data` in jobs

2026-10-22, personal account (bengal1), job 3967192.0, `jobs/data_rw.sub`.

**Answer: AF jobs mount `/data` read-write and write as the submitting user.** The job ran
as uid 13179, group `atlas-af` (5741). It wrote 100 MB under
`/data/bengal1/agentic-ops/phase0/`, read it back with a matching sha256, and deleted it.
New files get owner 13179:5741 and mode 644.

- `/data` is CephFS (`mds_namespace=work`), 5.8 PB, 63% full.
- `/data/agentic-ops`, the plan's shared root, does not exist. `/data` itself is owned by root,
  so the facility has to create it, with a group that both the robot identity and the people
  reading outputs belong to.
- `/data/<user>` is often a symlink: `/data/bengal1` points to `/cold/user/bengal1`, as do 252
  of the 728 user entries. `/cold` is another directory tree on the same CephFS cluster, and
  this test wrote there. Ask the facility whether `/cold` is a slower tier; production outputs
  and builds should go under a real `/data/agentic-ops`, not a user link.
- Rough timing (to `/cold`): write 1.5 s including generating the random data, read back
  0.4 s.
- The worker host was `workers-ads-5f8df59d6-2g76f`, which looks like the AF runs HTCondor
  workers as Kubernetes pods. Question 1's report will show the machine attributes.

Pending: repeat as the robot identity once question 2 says which local user remote
submissions map to, against `/data/agentic-ops` once it exists.

## 6. `af-io` start latency

`jobs/latency.sub` (20 jobs of 2 CPUs and 4 GB each), five runs, times in AF local time, from
the jobs' event logs (`probes/latency_report.py`):

| Submitted | Started | p50 | p90 | max |
|---|---|---|---|---|
| 2026-10-22 11:09 | 20/20 | 13 s | 13 s | 13 s |
| 2026-10-22 11:11 | 20/20 | 27 s | 32 s | 32 s |
| 2026-10-22 14:04 | 20/20 | 12 s | 12 s | 12 s |
| 2026-10-23 01:18 | 20/20 | 17 s | 17 s | 17 s |
| 2026-10-23 01:25 | 20/20 | 13 s | 13 s | 13 s |

**Answer: a small job starts within about 15 s (worst seen 32 s), day or night.** Start
latency alone does not justify keeping `af-io` workers warm; the keeper can start them on
demand and still alert on schedule-to-start. Caveat: the pool was far from full during these
runs (head04 had about 620 running and 34 idle jobs). The question 1 job on 2026-10-23 (1 CPU,
2 GB) waited 16 min 25 s, so the start time is not always this short.

The schedd's history on `head04` turns over within a day, so `latency_report.py` now reads
the jobs' event logs instead of `condor_history`.

## 7. Walltime and memory limits

2026-10-22, `probes/af_limits.sh` on login03, HTCondor 25.0.14, all slots AlmaLinux 9.

- **Machines** (partitionable): 73 × 36 cores / 89 GiB, 50 × 32 / 128 GiB, 22 × 80 / 256 GiB,
  10 × 80 / 238 GiB, 10 × 8 / 48 GiB, and a few others. The largest slot is 256 GiB.
- **Per job, enforced at submit:** at most 16 CPUs and 128 GB of memory.
- **Walltime:** there is no long queue to ask for. `SYSTEM_PERIODIC_HOLD_Long` holds any job
  that has run for 7 days. Jobs with `Queue == "short"` are held after 4 hours. Held or idle
  jobs are removed after 30 days. No retirement or max-runtime setting applies otherwise.
- **Memory default:** `JOB_DEFAULT_REQUESTMEMORY` is the job's past usage or image size, so
  every submission must set `request_memory`.
- **`/data` rules, enforced at submit:** the job's working directory may not be under
  `/data`; no `transfer_input_files` from `/data` or `/work`; no `transfer_output_files` to
  them; no job event log on them. The plan already fits: jobs read the tarballs from `/data`
  themselves and write outputs there directly. The pool keeper must keep the job's working
  directory and event log off `/data`.

Ben confirms: units of 2–4 hours of EWRun; workers that stop taking
units after 24 hours, far inside the 7-day hold, so an eviction or node drain loses at most a
unit and certificates can stay short-lived; memory classes of 4, 8 and 16 GB (EWKit's default
request is 2 GB), never `Queue = "short"`.
