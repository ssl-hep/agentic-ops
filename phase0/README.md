# Phase 0: settle the unknowns

Probes for the eight questions in the Phase 0 table of
[the production plan](../docs/temporal-production-plan.md#phase-0-settle-the-unknowns).
Results go in [phase0-results.md](../docs/phase0-results.md).

| Path | What |
|---|---|
| `../temporal-server/` | Go module: the claim mapper and authorizer (kept for phase 1), and a one-process test server |
| `certs/mkcerts.sh` | CA, server certificate and client certificates, into `secrets/phase0-certs/` |
| `local/compose.yaml` | The test server on this machine |
| `deploy/` | The test server on River, namespace `ai-ops` |
| `shim/` | Python: connection helper, namespace registration, `NoopWorkflow`, the no-op worker |
| `tests/test_authz.py` | Question 8 against a running server |
| `workerenv/build.sh` | The EL9 worker environment tarball (Python, temporalio, uproot, the shim) |
| `jobs/` | HTCondor jobs: `bootstrap.sh` and one submit file per question |
| `probes/` | Login-node and pod probes: AF limits, Rucio replicas (read-only), start latency, remote submission |

Certificates use `O=agentic-ops, OU=<role>, CN=<name>`. Roles: `admin`, `controller`,
`worker`. A worker certificate with `CN=ewkit-<production>` may poll only
`ewkit-<production>-<memclass>` and use only task tokens of `production/<production>` and
`dataset/<production>/...` workflows; `CN=af-io` may poll only `af-io`. Production IDs are
`[a-z0-9]+`, so no production's queues match another's.

## Locally

Needs Docker and uv. Go is not needed; it builds in Docker.

```bash
phase0/certs/mkcerts.sh
```

```bash
docker compose -f phase0/local/compose.yaml up -d --build
```

```bash
cd phase0 && uv run python -m shim.admin && uv run pytest
```

The no-op round trip by hand, in two terminals (from `phase0/`):

```bash
uv run python -m shim.noop_worker --certs ../secrets/phase0-certs/clients/ewkit-phase0
```

```bash
uv run python -m shim.start_noop
```

Go tests:

```bash
docker run --rm -v gomodcache:/go/pkg/mod -v "$PWD/temporal-server":/src -w /src golang:1.26 go test ./...
```

Worker environment (writes `phase0/workerenv/dist/worker-env.tar.gz` and its `.sha256`):

```bash
phase0/workerenv/build.sh
```

## On River

Pick the public host name and an image name you can push to. Both are used below:

```bash
export HOST=temporal-phase0.<domain> IMAGE=<registry>/agentic-ops-temporal-phase0:$(git rev-parse --short HEAD)
```

1. Server certificate with the public name, and the TLS Secret (kept in `secrets/`, like the
   other manifests):

   ```bash
   phase0/certs/mkcerts.sh -n "$HOST"
   ```

   ```bash
   kubectl -n ai-ops create secret generic temporal-phase0-tls --from-file=secrets/phase0-certs/server/ --dry-run=client -o yaml > secrets/temporal-phase0-tls.yaml && kubectl apply -f secrets/temporal-phase0-tls.yaml
   ```

2. Image for amd64, then the server and its ingress:

   ```bash
   docker buildx build --platform linux/amd64 -t "$IMAGE" --push temporal-server
   ```

   ```bash
   phase0/deploy/render.sh "$IMAGE" "$HOST" | kubectl apply -f -
   ```

3. **Is TLS passed through?** The certificate on port 443 must be ours:

   ```bash
   openssl s_client -connect "$HOST:443" -servername "$HOST" </dev/null 2>/dev/null | openssl x509 -noout -issuer
   ```

   Expect `issuer=O=agentic-ops, OU=phase0, CN=agentic-ops phase0 CA`. On River it is
   nginx's "Kubernetes Ingress Controller Fake Certificate": the controller runs without
   `--enable-ssl-passthrough`. Use the LoadBalancer Service instead, and remove the Ingress:

   ```bash
   kubectl apply -f phase0/deploy/service-lb.yaml && kubectl -n ai-ops delete ingress temporal-phase0
   ```

   The wildcard DNS name still points at the ingress, so clients connect to the load
   balancer's IP and verify the certificate against `$HOST`, which is already in it:

   ```bash
   export ADDRESS=$(kubectl -n ai-ops get svc temporal-phase0-public -o jsonpath='{.status.loadBalancer.ingress[0].ip}'):7233
   ```

   ```bash
   openssl s_client -connect "$ADDRESS" -servername "$HOST" </dev/null 2>/dev/null | openssl x509 -noout -issuer -ext subjectAltName
   ```

   The names must include `DNS:$HOST` exactly. Every command below connects to `$ADDRESS` and
   checks the certificate against `$HOST`. If passthrough is ever enabled, set
   `ADDRESS=$HOST:443` instead.

4. Namespace, then question 8 against the real endpoint:

   ```bash
   cd phase0 && uv run python -m shim.admin --address "$ADDRESS" --server-name "$HOST"
   ```

   ```bash
   PHASE0_ADDRESS="$ADDRESS" PHASE0_SERVER_NAME="$HOST" uv run pytest
   ```

   Denied worker calls are logged by the server (`Denied worker call`):

   ```bash
   kubectl -n ai-ops logs deploy/temporal-phase0 | grep "Denied worker call"
   ```

## On the AF

Copy the probes, the worker environment and the unit worker's certificate. `/data/$USER`
is used because `/data/agentic-ops` (the plan's root) does not exist yet; question 5 checks it.
`uchicago` is the ssh alias for `login.af.uchicago.edu`; set your AF user name:

```bash
export AF_USER=<your AF user name>
```

```bash
rsync -a phase0/jobs phase0/probes uchicago:phase0/
```

```bash
ssh uchicago 'mkdir -p /data/$USER/agentic-ops/builds/phase0 phase0/jobs/logs && install -d -m 700 phase0-certs'
```

```bash
scp phase0/workerenv/dist/worker-env.tar.gz* "uchicago:/data/$AF_USER/agentic-ops/builds/phase0/"
```

```bash
scp -r secrets/phase0-certs/clients/ewkit-phase0 uchicago:phase0-certs/
```

Everything below runs on the login node, from `~/phase0`, unless it says otherwise.

### Question 7: walltime and memory limits (run first: it also names the collector and schedd)

```bash
probes/af_limits.sh > af_limits.$(date +%F).txt
```

Done when a unit size and a worker walltime target are written into the results.

### Question 1: a job completes a no-op activity through the public endpoint

On the laptop, start the workflow; it waits up to six hours for a worker:

```bash
cd phase0 && uv run python -m shim.start_noop --address "$ADDRESS" --server-name "$HOST"
```

On the login node, with `HOST` and `ADDRESS` exported there too:

```bash
cd jobs && condor_submit noop.sub ADDRESS=$ADDRESS SERVER_NAME=$HOST DIGEST=$(cut -d' ' -f1 /data/$USER/agentic-ops/builds/phase0/worker-env.tar.gz.sha256)
```

Done when `start_noop` prints the report. It shows the worker host and OS, the bundled
Python, the job's walltime-related ClassAd attributes and the schedule-to-start time. The
job's `logs/noop.*.out` has the copy and unpack times.

### Question 5: `/data` in jobs

```bash
cd jobs && condor_submit data_rw.sub
```

Done when `logs/data_rw.*.out` shows the write, the matching checksums, the owner and the
delete. It also says whether `/data/agentic-ops` exists and is writable.

### Question 6: start latency of an af-io-sized job

Three times, at different times of day:

```bash
cd jobs && condor_submit latency.sub
```

```bash
python3 probes/latency_report.py <cluster>
```

### Question 3: the Rucio account and replica lookup

agentic-ops creates no replication rules; it only reads Rucio. With your account until the
robot exists (say so in the results):

```bash
setupATLAS && lsetup rucio && voms-proxy-init -voms atlas
```

```bash
python3 probes/rucio_replicas.py whoami
```

### Question 4: reading in place over `root://`

Pick a DID with replicas at MWT2 and at other sites; its size does not matter, only a few
files per site are read. `pfns` prints how its files are spread over sites, how many are
only on tape, and writes up to two files per site:

```bash
python3 probes/rucio_replicas.py pfns <scope:dataset> --out jobs/pfns.txt
```

The job copies each file to `/dev/null` directly and through the AF XCache, with and without
the proxy, and reports the rate:

```bash
cd jobs && condor_submit replica_read.sub
```

```bash
grep RESULT jobs/logs/replica_read.*.out
```

Done when MWT2 and at least two other sites have been read both ways. Run the job twice: the
second XCache read of a file shows the cached rate.

### Question 2: submitting from a pod with an IDTOKEN

Get a token for your identity on the login node (or from the AF admins for the robot), and
keep it out of anything readable by others:

```bash
condor_token_fetch -lifetime 86400 -token phase0 && cp ~/.condor/tokens.d/phase0 ~/phase0-token
```

Back on the laptop. The collector and schedd come from question 7: `COLLECTOR_HOST` and
`SCHEDD_HOST` on a login node (the login nodes submit to `head04`).

```bash
scp uchicago:phase0-token secrets/phase0-condor-token && ssh uchicago rm phase0-token && chmod 600 secrets/phase0-condor-token
```

```bash
kubectl -n ai-ops create secret generic phase0-condor-token --from-file=token=secrets/phase0-condor-token
```

```bash
kubectl -n ai-ops create configmap phase0-condor-probe --from-file=phase0/probes/condor_remote/submit_probe.py
```

```bash
kubectl -n ai-ops create configmap phase0-condor-probe-env --from-literal=AF_COLLECTOR=condor.af.uchicago.edu:9618 --from-literal=AF_SCHEDD=head04.af.uchicago.edu
```

```bash
kubectl apply -f phase0/probes/condor_remote/job.yaml && kubectl -n ai-ops logs -f job/phase0-condor-probe
```

Done when the log shows `MyRemoteUserName` from the ping, the job's `Owner` and
`AuthTokenSubject`, and the `id` output from the worker node. If the ping fails, the pod
cannot reach the schedd (port 9618) or the token is not accepted; set `CONDOR_DEBUG=1` and
`_condor_TOOL_DEBUG=D_SECURITY` in the env ConfigMap to see which.

## Teardown

```bash
kubectl -n ai-ops delete job/phase0-condor-probe deploy/temporal-phase0 svc/temporal-phase0 ingress/temporal-phase0 pvc/temporal-phase0-data secret/temporal-phase0-tls secret/phase0-condor-token configmap/phase0-condor-probe configmap/phase0-condor-probe-env --ignore-not-found
```

```bash
docker compose -f phase0/local/compose.yaml down -v
```

On the AF: `condor_rm` anything left, `rm -r /data/$USER/agentic-ops/phase0`, and
`condor_token_list` / remove the phase 0 token once question 2 is done.
