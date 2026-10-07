# agentic-ops

| Path | What |
|------|------|
| `frontend/` | Next.js (App Router, TypeScript) web app, port 3000 |
| `backend/` | FastAPI server (managed with uv), port 8000 |
| `charts/agentic-ops/` | Helm chart: frontend + backend + PostgreSQL ([groundhog2k/postgres](https://github.com/groundhog2k/helm-charts/tree/master/charts/postgres), official `postgres` image) |
| `docs/` | Project docs, including the [design proposal](docs/agentic-ops-proposal.html) |

## Campaign browser

The first screen of the app (the "Analyses" screen in the design proposal). It reads
[EWKit](https://gitlab.cern.ch/atlas-physics/hmbs/ewk/EWKit) through the GitLab API at any
branch, tag or commit and shows:

- every analysis folder under `data/` (except `common`), with its runnable status, configs,
  `Extend:` chain, sample lists and code owners from `CODEOWNERS`
- a campaign matrix per analysis: dataset counts for each sample list by MC campaign
  (`mc20a`…`mc23e`, worked out from the r-tag with the same map as EWKit's `EWGridRun.py`)
  and data year. Datasets whose r-tag is not in that map are flagged as `unknown`
- the datasets in a sample list, grouped by the list's `#` headings, filterable by campaign
  and name
- the config files, and whether EWKit's CI has pushed an image for the ref
  (`$CI_REGISTRY_IMAGE:$CI_COMMIT_REF_SLUG`), with its digest

A folder is **blocked** when its config (or one it extends) loads config blocks from
TopCPToolkit, which the standard EWKit image lacks; **config only** when it has no
`DAODs/Run*_*.txt` lists; **invalid** when its config does not parse or extends a missing file.

The backend only ever sends GET requests to GitLab. Anything read at a commit SHA is cached
in memory by that SHA; ref names and image tags are cached for 60 seconds. The browser never
talks to the backend or GitLab directly: Next.js server components call the backend at
`BACKEND_URL`.

Backend API (all take an optional `?ref=` branch, tag or SHA; the default branch otherwise):

| Endpoint | Returns |
|----------|---------|
| `GET /api/ewkit/status` | Whether the GitLab token is configured and the project is reachable |
| `GET /api/ewkit/refs` | Branches and tags |
| `GET /api/ewkit/analyses` | All analysis folders with summaries |
| `GET /api/ewkit/analyses/{folder}` | One folder |
| `GET /api/ewkit/analyses/{folder}/samples/{list}` | Parsed datasets in e.g. `Run3_bkg` |
| `GET /api/ewkit/analyses/{folder}/configs/{name}` | A config file's content |

Interactive docs are at `http://localhost:8000/docs` when the backend runs locally.

## GitLab token

The backend needs a GitLab token with the `read_api` scope to read EWKit, which is not
public. It never sends the token to the browser and never logs it. Keep it in a Kubernetes
Secret manifest in the gitignored `secrets/` directory at the top of this repo (or any other
directory outside version control).

### 1. Create the token

Pick one:

- **Project access token** (preferred, and the plan for any shared deployment). Needs the
  Maintainer role on EWKit, so usually an EWKit maintainer creates it for you. In EWKit, open
  **Settings → Access tokens → Add new token**, then set:
  - Token name: `agentic-ops` (it shows up as a bot user in EWKit's member list)
  - Expiration date: as far out as allowed (at most a year), and note it somewhere
  - Role: **Reporter**
  - Scopes: **read_api** only
- **Personal access token** (fine for a laptop-only setup). Open
  [Personal access tokens](https://gitlab.cern.ch/-/user_settings/personal_access_tokens) on
  gitlab.cern.ch and add a token with the **read_api** scope only.
  It can read everything your account can, so don't use it for a shared deployment.

Copy the token when GitLab shows it; it is shown only once.

### 2. Check it works

Paste the token at the prompt. `read -s` keeps it out of your shell history and off the
screen. (In zsh, `read -rs "GITLAB_TOKEN?GitLab token: "` prints a prompt too.)

```bash
read -rs GITLAB_TOKEN
```

```bash
curl -s --header "PRIVATE-TOKEN: $GITLAB_TOKEN" "https://gitlab.cern.ch/api/v4/projects/atlas-physics%2Fhmbs%2Fewk%2FEWKit" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("web_url", "token does not work"))'
```

This should print EWKit's URL. A `404 Project Not Found` means the token cannot see the project.

### 3. Write the Secret manifest

The token goes in through stdin, so it never shows up in the process list:

```bash
mkdir -p secrets && chmod 700 secrets
```

```bash
printf %s "$GITLAB_TOKEN" | kubectl create secret generic agentic-ops-gitlab --namespace agentic-ops --from-file=GITLAB_TOKEN=/dev/stdin --dry-run=client -o yaml > secrets/agentic-ops-gitlab.yaml && chmod 600 secrets/agentic-ops-gitlab.yaml
```

```bash
unset GITLAB_TOKEN
```

The manifest only base64-encodes the token, it does not encrypt it, so keep `secrets/` out
of git (already in `.gitignore`) and out of any cloud-synced folder.

### 4. Install it in the cluster

The namespace must exist before you apply the Secret. The first command is a no-op if it
already does:

```bash
kubectl create namespace agentic-ops --dry-run=client -o yaml | kubectl apply -f -
```

```bash
kubectl apply -f secrets/agentic-ops-gitlab.yaml
```

The backend reads the token from Secret `agentic-ops-gitlab`, key `GITLAB_TOKEN` (set by
`gitlab.existingSecret` and `gitlab.tokenKey` in `values.yaml`). The reference is optional,
so the backend starts without the Secret and the UI says the token is missing. Environment
variables are read only when a pod starts, so restart the backend after adding or replacing
the Secret:

```bash
kubectl -n agentic-ops rollout restart deployment/agentic-ops-backend
```

### Rotating the token

Before it expires, create a new token, repeat steps 2–4 (step 3 overwrites the manifest), then
revoke the old token in GitLab.

### Using the token outside the cluster

`uv run uvicorn` reads `backend/.env` (gitignored). Either write `GITLAB_TOKEN=...` there, or
copy the token out of the cluster each time you start the server:

```bash
cd backend && GITLAB_TOKEN="$(kubectl -n agentic-ops get secret agentic-ops-gitlab -o jsonpath='{.data.GITLAB_TOKEN}' | base64 -d)" uv run uvicorn app.main:app --reload
```

The design proposal also lists a `read_registry` deploy token for pulling EWKit images on
HTCondor workers. The campaign browser does not need it: it reads image tags and digests
through the API with the `read_api` token.

## Local development

```bash
cd backend && uv run uvicorn app.main:app --reload   # http://localhost:8000/docs
```

```bash
cd backend && uv run pytest
```

```bash
cd frontend && npm install && npm run dev            # http://localhost:3000
```

The frontend calls the backend at `BACKEND_URL` (default `http://localhost:8000`). Backend
settings come from environment variables or `backend/.env`:

| Variable | Default |
|----------|---------|
| `GITLAB_URL` | `https://gitlab.cern.ch` |
| `GITLAB_PROJECT` | `atlas-physics/hmbs/ewk/EWKit` |
| `GITLAB_TOKEN` | none; see [GitLab token](#gitlab-token) |

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

After rebuilding the images, restart the pods so they pick up the new `:latest`:

```bash
kubectl -n agentic-ops rollout restart deployment/agentic-ops-backend deployment/agentic-ops-frontend
```

Then open the app with:

```bash
kubectl -n agentic-ops port-forward svc/agentic-ops-frontend 3000:3000
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
