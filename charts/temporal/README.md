# Temporal server chart

The agentic-ops Temporal server (phase 1 of
[the production plan](../../docs/temporal-production-plan.md)): every Temporal service in one
process with the worker-only authorizer from `temporal-server/authz`, PostgreSQL from the
[groundhog2k `postgres` chart](https://github.com/groundhog2k/helm-charts/tree/master/charts/postgres)
(the official `docker.io/postgres` image; nothing from bitnami), and the frontend on a
LoadBalancer with mTLS.

What it deploys:

- **Server**: one pod (`replicas: 1`, `Recreate`): the config keeps every service except the
  frontend on localhost. Init containers run `temporal-sql-tool` from the same image to set up
  and upgrade the `temporal` and `temporal_visibility` schemas on every start; both steps are
  no-ops when the schema is current.
- **Postgres**: StatefulSet on `rook-ceph-block`, the PVC retained on uninstall. Credentials are
  generated into Secret `temporal-db-credentials` and kept across upgrades and uninstalls.
  `temporal` is the subchart's user database; `temporal_visibility` is created by an init
  script on the first start.
- **Endpoint**: LoadBalancer Service `<release>-temporal-public` on 7233 (gRPC only), an
  in-cluster Service with metrics on 9090, and ConfigMap `<release>-temporal-endpoint` with
  what clients need. An Ingress with TLS passthrough is in the chart, off.
- **Backups**: daily `pg_dump -Fc` of both databases into PVC `<release>-temporal-backup`.
- **Web UI** (optional): `temporalio/ui` with a `reader` certificate, behind the nginx ingress
  with basic auth; see [Web UI](#web-ui).
- **Metrics**: scrape annotations on the server pod and an optional ServiceMonitor; see
  [Metrics](#metrics).

The image is `bengal1/agentic-ops-temporal-server:<branch>` on Docker Hub, built and pushed by
`.github/workflows/temporal-server-image.yml` on every push to any branch (`/` in a branch name
becomes `-`). The chart follows the `temporal` branch by default and pulls on every start.

## Endpoint: temporal.ndslabs.org:31644 (NodePort, for now)

The frontend is the LoadBalancer Service `temporal-public`: 192.170.236.183:7233 (pinned by
`publicService.loadBalancerIP`), and NodePort 31644 on every node (pinned by
`publicService.nodePort`). Since 2026-11-19 the LoadBalancer address times out from outside
the cluster, whichever node MetalLB announces it from, while the NodePort works; that is with
the River admins. Until it is fixed, `temporal.ndslabs.org` has A records for node IPs
(`river-c031` 192.170.236.131, `river-c055` 192.170.236.155) and clients dial port 31644. Once
the LoadBalancer answers again, point the record back at 192.170.236.183 and set
`endpoint.address=temporal.ndslabs.org:7233`; no certificate change is needed either way,
since clients check the name.

Keep the record DNS only: a proxy in front (such as Cloudflare's) would terminate TLS and break
the authorizer. River's ingress cannot pass TLS through, so the Service is the only way in. The
address dialled and the name the certificate is checked against are separate settings
everywhere, though today both use `temporal.ndslabs.org`:

| Value | ConfigMap key | Client setting |
|---|---|---|
| `endpoint.address` (`<host>:<port>`) | `TEMPORAL_ADDRESS` | address dialled (`--address`, `PHASE0_ADDRESS`) |
| `endpoint.serverName` | `TEMPORAL_TLS_SERVER_NAME` | TLS server name (`--server-name`, `PHASE0_SERVER_NAME`) |
| `temporal.namespace` | `TEMPORAL_NAMESPACE` | namespace (`PHASE0_NAMESPACE`) |

If `endpoint.address` is empty the chart fills `TEMPORAL_ADDRESS` from the Service's
LoadBalancer status, which exists only after the first install. `publicService.loadBalancerIP`
asks for a particular address, so a reinstall keeps it; if the address ever changes, update the
DNS record.

With passthrough, if River enables it: `passthroughIngress.enabled=true`,
`passthroughIngress.host=<name>`, and `endpoint.address` empty or `<name>:443`. Whenever the
endpoint changes, rerun `phase0/tests/test_authz.py` and the AF no-op job through it.

## Install

All commands from the repository root, namespace `ai-ops`.

### Certificates

`agentic-ops-pki` ([pki/](../../pki/README.md)) keeps the CA in `secrets/temporal-pki`
(gitignored): an offline root, a worker-only intermediate CA, the server certificate and the
admin, controller and reader certificates. The commands that sign with the root ask for its
passphrase (or read `AGENTIC_OPS_PKI_PASSPHRASE`). Once:

```bash
cd pki && uv run agentic-ops-pki root && uv run agentic-ops-pki worker-ca
```

The server certificate must carry `endpoint.serverName`; add the LoadBalancer IP too (`-i`) so
clients may also verify against the IP. Re-run this whenever the worker CA is rotated: the
server directory also holds the worker CAs the server trusts.

```bash
cd pki && uv run agentic-ops-pki server -n temporal.ndslabs.org -i 192.170.236.183
```

```bash
cd pki && uv run agentic-ops-pki client --role admin --name admin && uv run agentic-ops-pki client --role controller --name controller && uv run agentic-ops-pki client --role reader --name web-ui
```

```bash
kubectl -n ai-ops create secret generic temporal-tls --from-file=secrets/temporal-pki/server/ --dry-run=client -o yaml | kubectl apply -f -
```

The server reads the Secret at start and refuses to start without `worker-ca.pem`, so update
the Secret before the first start of an image with the issuer checks, and restart after every
change (`kubectl -n ai-ops rollout restart deploy/temporal`). Afterwards move
`secrets/temporal-pki/root/ca-key.pem` offline.

The worker CA goes to the ops-executor (phase 3), which signs one worker certificate per
submission, as Secret `temporal-worker-ca` (`ca.pem`, `ca-key.pem`, `root.pem`; the
`retired/` subdirectory is left out):

```bash
kubectl -n ai-ops create secret generic temporal-worker-ca --from-file=secrets/temporal-pki/worker-ca/ --dry-run=client -o yaml | kubectl apply -f -
```

Until there is an ops-executor, sign worker certificates by hand, e.g. for the AF no-op job
(`phase0/jobs/noop.sub`, then copy the directory to `~/phase0-certs/ewkit-phase0` on the AF):

```bash
cd pki && uv run agentic-ops-pki worker --production phase0 --out ../secrets/temporal-pki/jobs/ewkit-phase0
```

### Chart

```bash
helm dependency build charts/temporal
```

```bash
helm upgrade --install temporal charts/temporal -n ai-ops
```

On a new cluster, read the LoadBalancer address, point the DNS record at it, and set
`publicService.loadBalancerIP` to it so a reinstall keeps it:

```bash
kubectl -n ai-ops get svc temporal-public -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

Check the schema steps and the server:

```bash
kubectl -n ai-ops logs deploy/temporal -c schema-update-temporal
```

```bash
kubectl -n ai-ops logs deploy/temporal -c temporal --tail=50
```

Upgrade with `--reset-then-reuse-values`, not `--reuse-values`: the latter keeps only the
values of the previous release, so keys a newer chart adds (such as `webUi.image`) are missing
and the templates fail on them.

After a new image is pushed, restart to pull it:

```bash
kubectl -n ai-ops rollout restart deploy/temporal
```

### Namespace

Registered with the admin certificate from the operator's machine; the admin certificate never
goes into the cluster.

```bash
cd phase0 && uv run python -m shim.admin --address temporal.ndslabs.org:31644 --certs ../secrets/temporal-pki/clients/admin --namespace ewkit-production --retention-days 30 --description "EWKit ntuple production" --search-attributes
```

### Check the authorizer through the public endpoint

`test_authz.py` needs a set of short-lived clients from this CA, including the ones that must be
refused (a worker certificate signed by the root, a controller signed by the worker CA, and one
from a foreign CA). They go in `secrets/temporal-pki/test`, apart from the real clients:

```bash
cd pki && uv run agentic-ops-pki test-clients
```

```bash
cd phase0 && PHASE0_ADDRESS=temporal.ndslabs.org:31644 PHASE0_NAMESPACE=ewkit-production PHASE0_CERTS=../secrets/temporal-pki/test uv run pytest
```

Every denial is in the server log with the certificate subject: `Denied worker call` and
`Denied call` from the authorizer, `Authorization error` for a certificate from the wrong CA.

```bash
kubectl -n ai-ops logs deploy/temporal -c temporal | grep -E 'Denied|Authorization error'
```

## Web UI

Off by default (`webUi.enabled`). The `temporalio/ui` pod reaches the frontend over the
in-cluster Service with the `reader` certificate: it can list and read workflows in
`temporal.namespace` and nothing else. People reach it through the nginx ingress at
`webUi.ingress.host` with **basic auth**. TLS ends at the ingress, which is fine here: the
client certificate the authorizer needs is the UI's own, on the hop behind it.

Readers also get three cluster-scoped reads the UI makes on every page (`GetClusterInfo`,
`ListNamespaces`, `GetSearchAttributes`; `temporal-server/authz/authorizer.go`). Temporal's
default authorizer allows those only to a system role, which would also cover every namespace.

The reader certificate (issued in [Certificates](#certificates)) as a Secret:

```bash
kubectl -n ai-ops create secret generic temporal-web-ui-tls --from-file=secrets/temporal-pki/clients/web-ui/ --dry-run=client -o yaml | kubectl apply -f -
```

The basic-auth users, as an htpasswd file under the key `auth` (`htpasswd` ships with macOS;
`-B` for bcrypt, which ingress-nginx accepts). Add `-c` only for the first user:

```bash
htpasswd -B -c secrets/temporal-ui-htpasswd <user>
```

```bash
kubectl -n ai-ops create secret generic temporal-ui-basic-auth --from-file=auth=secrets/temporal-ui-htpasswd --dry-run=client -o yaml | kubectl apply -f -
```

```bash
helm upgrade temporal charts/temporal -n ai-ops --reset-then-reuse-values --set webUi.enabled=true
```

cert-manager issues the ingress certificate from `webUi.ingress.clusterIssuer`
(`letsencrypt-prod`) into Secret `temporal-ui-tls`. Until it has, nginx serves its "Kubernetes
Ingress Controller Fake Certificate". The HTTP-01 challenge goes through a separate solver
Ingress that cert-manager creates (no `http01-edit-in-place`), because the challenge would
fail basic auth inside ours. Watch the certificate:

```bash
kubectl -n ai-ops get certificate,challenge
```

```bash
kubectl -n ai-ops describe certificate temporal-ui-tls
```

Check it: without credentials the host answers 401. With credentials it opens on
`ewkit-production`, and the server log has no denial for `CN=web-ui`:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://temporal-ui.servicex.ssl-hep.org/
```

```bash
kubectl -n ai-ops logs deploy/temporal -c temporal | grep -E 'Denied' | grep 'CN=web-ui'
```

## Metrics

The server exports Prometheus metrics on 9090 (`/metrics`), on the in-cluster Service only,
never on the public one. The server pod carries `prometheus.io/scrape|port|path` annotations
(`metrics.podAnnotations`) for a Prometheus that discovers pods that way. With the Prometheus
Operator, set `metrics.serviceMonitor.enabled=true` (and any labels its Prometheus selects on
in `metrics.serviceMonitor.labels`). To check what is there before Prometheus is wired up:

```bash
kubectl -n ai-ops port-forward svc/temporal 9090:9090
```

```bash
curl -s localhost:9090/metrics | grep -c '^temporal_'
```

## Backups

Run one now:

```bash
kubectl -n ai-ops create job --from=cronjob/temporal-backup temporal-backup-manual
```

Restore into a scratch database (the restore test from the phase 1 checklist; done
2026-11-19). It needs a pod that mounts the backup PVC (read-only) and connects as the
superuser, because the `temporal` user cannot create databases. The PVC is ReadWriteOnce, so
don't start the pod while a backup job is running:

```bash
kubectl -n ai-ops apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata: {name: temporal-restore-test}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext: {runAsNonRoot: true, runAsUser: 999, runAsGroup: 999, fsGroup: 999, seccompProfile: {type: RuntimeDefault}}
  containers:
    - name: restore
      image: docker.io/postgres:18.6
      command: [sleep, "3600"]
      env:
        - {name: PGUSER, value: postgres}
        - {name: PGDATABASE, value: postgres}
        - {name: PGPASSWORD, valueFrom: {secretKeyRef: {name: temporal-db-credentials, key: POSTGRES_PASSWORD}}}
        - {name: PGHOST, valueFrom: {secretKeyRef: {name: temporal-db-credentials, key: DB_HOST}}}
        - {name: PGPORT, valueFrom: {secretKeyRef: {name: temporal-db-credentials, key: DB_PORT}}}
      securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
      volumeMounts:
        - {name: backup, mountPath: /backup, readOnly: true}
        - {name: tmp, mountPath: /tmp}
  volumes:
    - {name: backup, persistentVolumeClaim: {claimName: temporal-backup, readOnly: true}}
    - {name: tmp, emptyDir: {}}
EOF
```

```bash
kubectl -n ai-ops exec temporal-restore-test -- bash -c 'createdb temporal_restore_test && pg_restore --no-owner --exit-on-error -d temporal_restore_test /backup/temporal-<stamp>.dump && psql -d temporal_restore_test -c "select * from schema_version"'
```

Compare row counts with the live `temporal` database (e.g. `current_executions`,
`history_node`), then clean up:

```bash
kubectl -n ai-ops exec temporal-restore-test -- dropdb temporal_restore_test && kubectl -n ai-ops delete pod temporal-restore-test
```

To restore for real, scale the server to zero first
(`kubectl -n ai-ops scale deploy/temporal --replicas=0`), restore over `temporal` and
`temporal_visibility` with `pg_restore --clean`, then scale back to one.

## Uninstall

`helm uninstall temporal -n ai-ops` keeps the credentials Secret, the Postgres PVC
(`postgres-data-temporal-postgres-0`) and the backup PVC, so a reinstall finds its data.
Delete them by hand to start over.
