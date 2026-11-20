# agentic-ops-worker

Temporal workflows and activities for EWKit ntuple production
([plan](../docs/temporal-production-plan.md), phase 2). So far, steps 1 (the skeleton), 2 (pure
logic, no Temporal) and 3 (the reporting schema):

| Module | Contents |
|---|---|
| `names.py` | Task queues (`controller`, `ops`, `triage`, `af-io`, `ewkit-<production>-<memclass>`), workflow IDs (`production/<id>`, `dataset/<id>/<did>`, `pool-keeper/<id>`), production ID and DID checks. The server's authorizer depends on these shapes |
| `contracts.py` | Payloads shared with EWKit's worker code: `UnitSpec`, `Manifest`, the `af-io` requests and results, error type names. [`contract-examples/`](contract-examples) has one JSON file per payload |
| `search_attributes.py` | The custom search attributes, typed. Registered in `ewkit-production` by `phase0/shim/admin.py --search-attributes` |
| `units.py` | Unit states and their transitions, `Unit` (as in the `units` table) and `UnitSummary` (what a workflow keeps), memory-class escalation, the coverage invariant |
| `planning.py` | `plan_units` from file sizes and first-read rates (tape-only files into `BLOCKED` units), unit IDs, `split`, `split_even`, `exclude_files`, `unblock` |
| `failures.py` | Failure classes of `run_unit` and the workflow's next step after each: retry with backoff (refreshing replicas first for unreadable inputs), block, or triage |
| `signatures.py` | Triage proposals (the closed set of actions) and the fixed rules: out of memory, walltime, corrupt `TFile`, xrootd `[3010]` |
| `records.py` | What workflows record in the reporting schema: the `controller` activity names and their payloads, and the idempotency rules |
| `reporting.py` | The reporting writes and reads over Postgres, and `ReportingActivities`. A write the schema refuses fails as non-retryable `ReportingIntegrity` |
| `migrate.py`, `migrations/` | The reporting schema (`production`: tables, and the invariant and what-remains views), applied by `agentic-ops-migrate --dsn …` |

`units.py`, `planning.py`, `failures.py`, `signatures.py` and `records.py` are pure: stdlib only,
deterministic, no I/O (`tests/test_pure.py`), so workflow code can import them and they are tested
with no server. Workflow code never imports `reporting.py`. `tests/test_units.py` runs a hypothesis
state machine of splits, exclusions, stagings and attempts, checking after every step that each
snapshot file is in exactly one live unit.

```bash
cd worker && uv sync && uv run pytest
```

The tests run every Temporal test in two environments (`tests/conftest.py`): the time-skipping
test server and the local dev server. The SDK downloads both binaries on first use.
`tests/test_environments.py` records what each supports:

| | time-skipping | local dev server |
|---|---|---|
| Activities by name on another queue | yes | yes |
| Updates with validators | yes | yes |
| Child workflows | yes | yes |
| Search attribute upserts, read back with describe | yes, once registered with `AddSearchAttributes` | yes |
| Visibility queries (`ListWorkflowExecutions`) | no (unimplemented) | yes |
| Task queue backlog (`DescribeTaskQueue` with stats) | no (unimplemented) | yes |
| Timers skipped | yes | no |

The default data converter cannot encode a `timedelta`: payloads carry durations as seconds.

The reporting tests need Docker: they start `postgres:18` (the chart's version) once per session
through testcontainers and give each test a database cloned from a migrated template. Without
Docker they skip, except when `CI` is set. `tests/test_reporting_coverage.py` runs a hypothesis
state machine that checks that the SQL invariants agree with `units.coverage_violations`.
