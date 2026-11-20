"""Migrations apply once, also when two runners race, and the schema's value lists match the
Python enums they mirror."""

import asyncio
import re
from collections.abc import AsyncIterator

import psycopg
import pytest
from conftest import Postgres

from agentic_ops_worker import records as r
from agentic_ops_worker.failures import FailureClass
from agentic_ops_worker.migrate import migrate, migrations
from agentic_ops_worker.names import MEMCLASSES
from agentic_ops_worker.signatures import Action
from agentic_ops_worker.units import UnitState


@pytest.fixture
async def empty(postgres: Postgres) -> AsyncIterator[str]:
    name = await postgres.create_database()
    yield postgres.dsn(name)
    await postgres.drop_database(name)


def test_migration_files() -> None:
    names = [name for _, name, _ in migrations()]
    assert names[0] == "0001_reporting.sql"
    assert [v for v, _, _ in migrations()] == list(range(1, len(names) + 1))


async def test_applies_once(empty: str) -> None:
    all_names = [name for _, name, _ in migrations()]
    async with await psycopg.AsyncConnection.connect(empty, autocommit=True) as conn:
        assert await migrate(conn) == all_names
        assert await migrate(conn) == []
        cur = await conn.execute("SELECT name FROM production.schema_migrations ORDER BY version")
        assert [row[0] for row in await cur.fetchall()] == all_names


async def test_concurrent_runners(empty: str) -> None:
    all_names = [name for _, name, _ in migrations()]
    async with (
        await psycopg.AsyncConnection.connect(empty, autocommit=True) as a,
        await psycopg.AsyncConnection.connect(empty, autocommit=True) as b,
    ):
        results = await asyncio.gather(migrate(a), migrate(b))
    assert sorted(results, key=len) == [[], all_names]


ENUM_CHECKS = {
    "units_state_check": {s.value for s in UnitState},
    "units_memclass_check": set(MEMCLASSES),
    "attempts_memclass_check": set(MEMCLASSES),
    "attempts_failure_class_check": {c.value for c in FailureClass},
    "attempts_outcome_check": {o.value for o in r.AttemptOutcome},
    "attempts_verification_check": {v.value for v in r.Verification},
    "decisions_action_check": {a.value for a in Action},
    "decisions_verdict_check": {v.value for v in r.Verdict},
    "productions_state_check": {s.value for s in r.ProductionState},
    "datasets_state_check": {s.value for s in r.DatasetState},
}


@pytest.mark.parametrize("constraint", sorted(ENUM_CHECKS))
async def test_check_lists_match_enums(conn: psycopg.AsyncConnection, constraint: str) -> None:
    cur = await conn.execute(
        "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace"
        " WHERE n.nspname = 'production' AND c.conname = %s",
        (constraint,),
    )
    row = await cur.fetchone()
    assert row, f"no constraint {constraint}"
    assert set(re.findall(r"'([^']*)'::text", row[0])) == ENUM_CHECKS[constraint]
