# agentic-ops

| Path | What |
|------|------|
| `frontend/` | Next.js (App Router, TypeScript) hello-world app, port 3000 |
| `backend/` | FastAPI server (managed with uv), `GET /healthz`, port 8000 |
| `charts/agentic-ops/` | Helm chart: frontend + backend + PostgreSQL ([groundhog2k/postgres](https://github.com/groundhog2k/helm-charts/tree/master/charts/postgres), official `postgres` image) |
| `docs/` | Project docs |

## Local development

```bash
cd backend && uv run uvicorn app.main:app --reload   # http://localhost:8000/healthz
cd backend && uv run pytest
cd frontend && npm install && npm run dev            # http://localhost:3000
```

## Container images

```bash
docker build -t agentic-ops-backend:latest backend
docker build -t agentic-ops-frontend:latest frontend
```

## Helm

```bash
helm dependency update charts/agentic-ops
helm install agentic-ops charts/agentic-ops -n agentic-ops --create-namespace
```

Image repository/tag are set under `backend.image` and `frontend.image` in `values.yaml`.
The defaults use the locally built `:latest` images, which works on clusters that share
the Docker image store (OrbStack, Docker Desktop). For other clusters, push to a registry
and override them.

### Database credentials

The chart installs a Secret (default name `agentic-ops-db-credentials`, set by
`postgres.settings.existingSecret`) with:

| Key | Purpose |
|-----|---------|
| `POSTGRES_PASSWORD` | Postgres superuser password |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Application database connection details |
| `DATABASE_URL` | `postgresql://user:password@host:5432/db` |

The Postgres subchart creates its superuser and the application database/user from this
Secret, and the backend gets all non-superuser keys as environment variables.

Passwords are randomly generated on first install and reused on upgrade, unless you set
`database.password` / `database.superuserPassword`. The Secret has
`helm.sh/resource-policy: keep`, so it survives `helm uninstall` and stays in step with the
Postgres data volume, which is also kept. Delete both by hand if you want a clean reinstall.

```bash
kubectl -n agentic-ops get secret agentic-ops-db-credentials -o jsonpath='{.data.DATABASE_URL}' | base64 -d
```
