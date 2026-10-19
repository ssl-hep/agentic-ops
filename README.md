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
talks to the backend or GitLab directly: Next.js server components (and, for run setup, server
actions) call the backend at `BACKEND_URL`.

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

## New run: the run's datasets

For now **New run** settles one thing: which datasets the run reads. Describe the data in plain
words, such as "Run 3 Z+jets background and data for RadiativeDecays at EFP_v6.6". Each request is
one LangGraph thread (`backend/app/agents/request_graph.py`):

1. **Dataset builder agent** (optional, `backend/app/agents/dataset_builder.py`) picks sample lists
   and EWKit-style `-w`/`--skip` regexes, using the same read-only repository tools. It never
   writes dataset names; code (`backend/app/run_request.py`) expands its queries at the pinned
   commit into the list, with the file and line each dataset came from.
2. **The person** edits the list (or lists datasets by hand when the agents are off), or changes
   the request and redrafts.
3. **Spec is ready** hands the final list to `SpecReadyService` (`backend/app/ops.py`), a
   procedural service with no model or tool access. It is closed over by the graph's
   deterministic submit node only, never put in the context the agent nodes see. For now it
   logs the datasets on the `app.ops.spec_ready` logger, in the backend's output.

| Endpoint | Does |
|----------|------|
| `POST /api/requests` | Start a request from `{ref, analysis, request}` and run it to its first pause |
| `GET /api/requests/{id}` | The request: datasets, builder queries and notes, receipt, history |
| `POST /api/requests/{id}/actions` | `{"action": "edit", "datasets": [...]}`, `{"action": "redraft", "request": "..."}` or `{"action": "finish"}` |

The harness's `run-request` agent (`harness/README.md`) is the place to try the same job with
Rucio checks, such as which datasets are already at MWT2.

## Run setup

The full run options. Start it with **Set up a run** on a runnable analysis. Nothing is
submitted from here: the result is a checked `RunSpec`, the options a production passes to
`EWRun.py`, mirroring `EWGridRun.py` (analysis, `--configName`, runs, sample lists,
`--doSys`/`--noSys`, `--noFilter`, `--noThinning`, `-w`/`--skip` patterns, purpose, memory).

Each setup is one LangGraph thread (`backend/app/agents/graph.py`):

1. **Builder agent** (optional) drafts the `RunSpec` from the request, using read-only tools
   over the repository connector. It gives a reason for each field, lists its assumptions and
   asks questions where the request is ambiguous.
2. **Validator** (code, `backend/app/validator.py`) checks the spec at the pinned commit and
   returns findings with codes and evidence. It alone decides what passes.
3. **Reviewer agent** turns the findings into issue cards with evidence and an optional
   `RunSpec` patch. It sees the request, the spec and the findings, not the builder's reasoning.
   Code drops cards that cite neither a finding nor a file at the commit, checks every patch
   against the schema, and dry-runs it through the validator to show what it would clear.
4. **The person** accepts, edits or dismisses each card, or edits the form directly. Accepted
   patches and edits go back through the validator and the reviewer. **Spec is ready** is
   allowed once there are no blocking findings.

The form is the one editor of the spec; the builder only fills it in. Neither agent can write to
EWKit, edit configs or sample lists, or decide that a check passed. Repository text reaches the
models only inside `<evidence>` blocks. The model, the prompt versions and every accepted or
dismissed patch are kept in the setup's history. Without a model endpoint the agents are off,
and the form and validator work the same way.

Validator finding codes:

| Severity | Codes |
|----------|-------|
| blocking | `ANALYSIS_NOT_FOUND`, `FOLDER_NOT_RUNNABLE`, `CONFIG_NOT_FOUND`, `CONFIG_PARSE_ERROR`, `EXTEND_MISSING`, `CONFIG_NEEDS_UNAVAILABLE_MODULES`, `NO_RUNS`, `RUN_NOT_AVAILABLE`, `NO_SAMPLE_LISTS`, `SAMPLE_LIST_NOT_FOUND`, `EMPTY_SELECTION`, `UNKNOWN_RTAG`, `UNRECOGNISED_DATASET`, `INVALID_PATTERN`, `REF_POLICY` (full productions need a tag), `REF_NOT_FOUND` |
| warning | `SAMPLE_LIST_MISSING_FOR_RUN`, `EMPTY_LIST`, `EXTEND_READONLY_IMAGE`, `NO_IMAGE_FOR_REF`, `MEMORY_OUT_OF_RANGE` |
| info | `NOT_IN_CI`, `SYSTEMATICS_DEFAULT_ON`, `SYSTEMATICS_OVERRIDDEN`, `IMAGE_UNCHECKED` |

Rucio resolution, the image pull and `EWRun.py -PS` checks come with Gate 1.

Threads are checkpointed in Postgres when `DATABASE_URL` is set (always, in the Helm chart), and
in memory otherwise, so local setups without a database are lost when the backend restarts.

| Endpoint | Does |
|----------|------|
| `GET /api/setups/agents` | Whether the agents are on, and which endpoint and models they use; `?check=true` also calls `GET {base}/models` |
| `POST /api/setups` | Start a setup from `{ref, analysis, request, spec}` and run it to its first pause |
| `GET /api/setups/{id}` | The setup: spec, builder notes, findings, issue cards, history |
| `POST /api/setups/{id}/actions` | `{"action": "apply", "accept": [...], "dismiss": [...]}`, `{"action": "edit", "spec": {...}}`, `{"action": "redraft", "request": "..."}` or `{"action": "finish"}` |

A call that runs the agents can take a minute or two, depending on the model.

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

## Model endpoint

The run setup agents talk to any **OpenAI-compatible** chat completions endpoint: an
institutional LLM gateway, a LiteLLM proxy, vLLM, Ollama, or OpenAI itself. They need:

- a **base URL** up to and including `/v1`, e.g. `https://llm.example.org/v1`
- a **model** name the endpoint serves. It must support **tool (function) calling**, which the
  agents use both to read the repository and to return structured answers
- a **token**, sent as `Authorization: Bearer <token>`. Leave it out for endpoints without auth

Request texts, run options, dataset names and config excerpts are sent to this endpoint. Check
that this is allowed for the service you pick before you use it with EWKit (decision D13 in the
proposal).

Like the GitLab token, the model token lives in a Kubernetes Secret written to the gitignored
`secrets/` directory. The base URL and model are not secret and go in the Helm values.

### 1. Check the endpoint works

```bash
read -r "LLM_BASE_URL?Base URL (ending in /v1): " && read -r "LLM_MODEL?Model: " && read -rs "LLM_API_KEY?Token: "
```

(That is zsh; in bash use `read -rp "Base URL: " LLM_BASE_URL` and so on, with `-rsp` for the
token.) List the models the token can use:

```bash
curl -s "$LLM_BASE_URL/models" -H "Authorization: Bearer $LLM_API_KEY" | python3 -c 'import json,sys; print([m["id"] for m in json.load(sys.stdin)["data"]])'
```

Then check that the model makes tool calls, the way the agents ask for structured answers:

```bash
curl -s "$LLM_BASE_URL/chat/completions" -H "Authorization: Bearer $LLM_API_KEY" -H "Content-Type: application/json" -d '{"model": "'"$LLM_MODEL"'", "messages": [{"role": "user", "content": "Which EWKit run: Run 3 background for RadiativeDecays"}], "tools": [{"type": "function", "function": {"name": "RunSpec", "parameters": {"type": "object", "properties": {"analysis": {"type": "string"}, "runs": {"type": "array", "items": {"type": "string"}}}, "required": ["analysis", "runs"]}}}], "tool_choice": {"type": "function", "function": {"name": "RunSpec"}}}' | python3 -c 'import json,sys; m=json.load(sys.stdin)["choices"][0]["message"]; print(m.get("tool_calls") or "NO TOOL CALL: " + str(m.get("content")))'
```

This should print a `RunSpec` tool call. If the endpoint rejects the named `tool_choice`, set
`llm.structuredOutput` to `json_schema` (OpenAI-style structured outputs) or `json_mode`.

### 2. Write the Secret manifest

```bash
mkdir -p secrets && chmod 700 secrets
```

```bash
printf %s "$LLM_API_KEY" | kubectl create secret generic agentic-ops-llm --namespace agentic-ops --from-file=LLM_API_KEY=/dev/stdin --dry-run=client -o yaml > secrets/agentic-ops-llm.yaml && chmod 600 secrets/agentic-ops-llm.yaml
```

```bash
unset LLM_API_KEY
```

### 3. Install it and point the chart at the endpoint

```bash
kubectl apply -f secrets/agentic-ops-llm.yaml
```

```bash
helm upgrade agentic-ops charts/agentic-ops -n agentic-ops --reuse-values --set llm.baseUrl="$LLM_BASE_URL" --set llm.model="$LLM_MODEL"
```

The backend reads the token from Secret `agentic-ops-llm`, key `LLM_API_KEY` (set by
`llm.existingSecret` and `llm.tokenKey`). The reference is optional, so the backend starts
without it. Changing `llm.*` values rolls the backend by itself; after adding or replacing only
the Secret, restart it:

```bash
kubectl -n agentic-ops rollout restart deployment/agentic-ops-backend
```

Other settings under `llm:` in `values.yaml`: `builderModel` and `reviewerModel` (per-agent
overrides of `model`), `structuredOutput`, `temperature`, `timeoutSeconds` and `maxToolCalls`.
The New run page shows which endpoint and models are in use and whether `/models` answers.

### Rotating the token

Repeat steps 1–3 with the new token (step 2 overwrites the manifest), restart the backend, then
revoke the old token.

### Using the endpoint outside the cluster

Put the same settings in `backend/.env` (gitignored):

```
LLM_BASE_URL=https://llm.example.org/v1
LLM_MODEL=your-model
LLM_API_KEY=...
```

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
| `LLM_BASE_URL` | none: run setup agents off; see [Model endpoint](#model-endpoint) |
| `LLM_MODEL` | none |
| `LLM_API_KEY` | none |
| `LLM_BUILDER_MODEL`, `LLM_REVIEWER_MODEL` | `LLM_MODEL` |
| `LLM_STRUCTURED_OUTPUT` | `function_calling` (or `json_schema`, `json_mode`) |
| `LLM_TEMPERATURE`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_TOOL_CALLS` | `0`, `120`, `8` |
| `DATABASE_URL` | none: run setups are kept in memory |

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
