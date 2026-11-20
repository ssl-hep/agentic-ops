"""The two Temporal test environments, as a session fixture parametrized over both.

- time_skipping: Temporal's Java test server. Timers are skipped while a test waits on a result,
  so backoff, pool keeper intervals and the 15-minute triage window take no wall time. It
  implements no visibility queries and no task queue statistics (tests/test_environments.py).
- local: the Temporal CLI dev server, a real server with SQLite visibility, for the end-to-end
  simulation.

Both get the production search attributes registered, as ewkit-production has them. Both
download their server binary on first use and cache it in the system temp directory.

The reporting schema is tested against Postgres 18 (the chart's version) in Docker, started once
per session with testcontainers. Each test gets its own database, cloned from one migrated
template. Without Docker those tests skip, except on CI (CI set), where they fail.
"""

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest
from temporalio.testing import WorkflowEnvironment

from agentic_ops_worker import search_attributes as sa
from agentic_ops_worker.migrate import migrate

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


# --- Postgres ----------------------------------------------------------------------------------

POSTGRES_IMAGE = "postgres:18"
TEMPLATE = "reporting_template"


@dataclass
class Postgres:
    """A Postgres server for the session: create and drop databases on it."""

    url: str

    def dsn(self, dbname: str) -> str:
        return make_conninfo(self.url, dbname=dbname)

    async def create_database(self, template: str | None = None) -> str:
        name = f"t_{uuid.uuid4().hex}"
        async with await psycopg.AsyncConnection.connect(self.url, autocommit=True) as admin:
            await admin.execute(f"CREATE DATABASE {name}" + (f" TEMPLATE {template}" if template else ""))
        return name

    async def drop_database(self, name: str) -> None:
        async with await psycopg.AsyncConnection.connect(self.url, autocommit=True) as admin:
            await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture(scope="session")
async def postgres() -> AsyncIterator[Postgres]:
    from testcontainers.community.postgres import PostgresContainer

    container = PostgresContainer(POSTGRES_IMAGE, driver=None)
    try:
        container.start()
    except Exception as e:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"cannot start {POSTGRES_IMAGE} in Docker: {e}")
    try:
        server = Postgres(container.get_connection_url())
        async with await psycopg.AsyncConnection.connect(server.url, autocommit=True) as admin:
            await admin.execute(f"CREATE DATABASE {TEMPLATE}")
        async with await psycopg.AsyncConnection.connect(server.dsn(TEMPLATE), autocommit=True) as conn:
            await migrate(conn)
        yield server
    finally:
        container.stop()


@pytest.fixture
async def db(postgres: Postgres) -> AsyncIterator[str]:
    """The DSN of a fresh, migrated database."""
    name = await postgres.create_database(TEMPLATE)
    yield postgres.dsn(name)
    await postgres.drop_database(name)


@pytest.fixture
async def conn(db: str) -> AsyncIterator[psycopg.AsyncConnection]:
    async with await psycopg.AsyncConnection.connect(db, autocommit=True) as connection:
        yield connection
