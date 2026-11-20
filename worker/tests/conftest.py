"""The two Temporal test environments, as a session fixture parametrized over both.

- time_skipping: Temporal's Java test server. Timers are skipped while a test waits on a result,
  so backoff, pool keeper intervals and the 15-minute triage window take no wall time. It
  implements no visibility queries and no task queue statistics (tests/test_environments.py).
- local: the Temporal CLI dev server, a real server with SQLite visibility, for the end-to-end
  simulation.

Both get the production search attributes registered, as ewkit-production has them. Both
download their server binary on first use and cache it in the system temp directory.
"""

from collections.abc import AsyncIterator

import pytest
from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest
from temporalio.testing import WorkflowEnvironment

from agentic_ops_worker import search_attributes as sa

NAMESPACE = "ewkit-production"


async def start_environment(kind: str) -> WorkflowEnvironment:
    if kind == "local":
        return await WorkflowEnvironment.start_local(namespace=NAMESPACE, search_attributes=sa.ALL)
    env = await WorkflowEnvironment.start_time_skipping()
    # Without this the server fails every workflow task that upserts one of them, and the
    # workflow retries the task forever
    await env.client.operator_service.add_search_attributes(
        AddSearchAttributesRequest(
            namespace=env.client.namespace,
            search_attributes={key.name: key.indexed_value_type.value for key in sa.ALL},
        )
    )
    return env


@pytest.fixture(scope="session", params=["time_skipping", "local"])
async def env(request: pytest.FixtureRequest) -> AsyncIterator[WorkflowEnvironment]:
    async with await start_environment(request.param) as environment:
        yield environment
