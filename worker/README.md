# agentic-ops-worker

Temporal workflows and activities for EWKit ntuple production
([plan](../docs/temporal-production-plan.md), phase 2). So far, step 1, the skeleton:

| Module | Contents |
|---|---|
| `names.py` | Task queues (`controller`, `ops`, `triage`, `af-io`, `ewkit-<production>-<memclass>`), workflow IDs (`production/<id>`, `dataset/<id>/<did>`, `pool-keeper/<id>`), production ID and DID checks. The server's authorizer depends on these shapes |
| `contracts.py` | Payloads shared with EWKit's worker code: `UnitSpec`, `Manifest`, the `af-io` requests and results, error type names. [`contract-examples/`](contract-examples) has one JSON file per payload |
| `search_attributes.py` | The custom search attributes, typed. Registered in `ewkit-production` by `phase0/shim/admin.py --search-attributes` |

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
