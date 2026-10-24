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

The image is `bengal1/agentic-ops-temporal-server:<branch>` on Docker Hub, built and pushed by
`.github/workflows/temporal-server-image.yml` on every push to any branch (`/` in a branch name
becomes `-`). The chart follows the `temporal` branch by default and pulls on every start.

## Endpoint: an IP for now

There is no DNS record for the load balancer yet, and River's ingress cannot pass TLS through,
so clients dial the LoadBalancer **IP** and verify the server certificate against a **name**.
The two are separate settings everywhere:

| Value | ConfigMap key | Client setting |
|---|---|---|
| `endpoint.address` (`<ip>:7233`) | `TEMPORAL_ADDRESS` | address dialled (`--address`, `PHASE0_ADDRESS`) |
| `endpoint.serverName` | `TEMPORAL_TLS_SERVER_NAME` | TLS server name (`--server-name`, `PHASE0_SERVER_NAME`) |
| `temporal.namespace` | `TEMPORAL_NAMESPACE` | namespace (`PHASE0_NAMESPACE`) |

If `endpoint.address` is empty the chart fills `TEMPORAL_ADDRESS` from the Service's
LoadBalancer status, which exists only after the first install, so set it explicitly once you
know it. `publicService.loadBalancerIP` asks for a particular address, so a reinstall keeps it.

Later, with a DNS record: put the name in `endpoint.address` (`<name>:7233`). With passthrough:
`passthroughIngress.enabled=true`, `passthroughIngress.host=<name>`, and `endpoint.address`
empty or `<name>:443`. In both cases rerun `phase0/tests/test_authz.py` through the new
endpoint: anything that terminates TLS in front of the frontend breaks the authorizer.

## Install

All commands from the repository root, namespace `ai-ops`.

### Certificates

Same CA scheme as phase 0 (`phase0/certs/mkcerts.sh`). The server certificate must carry
`endpoint.serverName`; add the LoadBalancer IP too (`-i`) so clients may also verify against
the IP. If you do not know the IP yet, install first, then come back and re-run this with `-i`
(the CA is kept, so client certificates stay valid).

```bash
phase0/certs/mkcerts.sh -o secrets/temporal-certs -n temporal.servicex.ssl-hep.org -i <LB IP>
```

```bash
kubectl -n ai-ops create secret generic temporal-tls --from-file=secrets/temporal-certs/server/ --dry-run=client -o yaml | kubectl apply -f -
```

### Chart

```bash
helm dependency build charts/temporal
```

```bash
helm upgrade --install temporal charts/temporal -n ai-ops
```

Read the LoadBalancer address and record it in the release:

```bash
kubectl -n ai-ops get svc temporal-public -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

```bash
helm upgrade temporal charts/temporal -n ai-ops --reuse-values --set endpoint.address=<LB IP>:7233 --set publicService.loadBalancerIP=<LB IP>
```

Check the schema steps and the server:

```bash
kubectl -n ai-ops logs deploy/temporal -c schema-update-temporal
```

```bash
kubectl -n ai-ops logs deploy/temporal -c temporal --tail=50
```

After a new image is pushed, restart to pull it:

```bash
kubectl -n ai-ops rollout restart deploy/temporal
```

### Namespace

Registered with the admin certificate from the operator's machine; the admin certificate never
goes into the cluster.

```bash
cd phase0 && uv run python -m shim.admin --address <LB IP>:7233 --server-name temporal.servicex.ssl-hep.org --certs ../secrets/temporal-certs/clients/admin --namespace ewkit-production --retention-days 30 --description "EWKit ntuple production" --search-attributes
```

### Check the authorizer through the public endpoint

```bash
cd phase0 && PHASE0_ADDRESS=<LB IP>:7233 PHASE0_SERVER_NAME=temporal.servicex.ssl-hep.org PHASE0_NAMESPACE=ewkit-production PHASE0_CERTS=../secrets/temporal-certs uv run pytest
```

## Backups

Run one now:

```bash
kubectl -n ai-ops create job --from=cronjob/temporal-backup temporal-backup-manual
```

Restore into a scratch database (the restore test from the phase 1 checklist). From a pod
that mounts the backup PVC, e.g. a copy of the backup Job with `sleep infinity` as its command,
as the superuser (`PGUSER=postgres`, `PGPASSWORD` from `POSTGRES_PASSWORD` in the Secret; the
`temporal` user cannot create databases):

```bash
createdb temporal_restore_test && pg_restore --no-owner -d temporal_restore_test /backup/temporal-<stamp>.dump && psql -d temporal_restore_test -c 'select * from schema_version'
```

To restore for real, scale the server to zero first
(`kubectl -n ai-ops scale deploy/temporal --replicas=0`), restore over `temporal` and
`temporal_visibility` with `pg_restore --clean`, then scale back to one.

## Uninstall

`helm uninstall temporal -n ai-ops` keeps the credentials Secret, the Postgres PVC
(`postgres-data-temporal-postgres-0`) and the backup PVC, so a reinstall finds its data.
Delete them by hand to start over.
