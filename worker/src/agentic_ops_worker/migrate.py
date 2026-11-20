"""Apply the reporting schema's migrations (migrations/NNNN_<name>.sql, in order).

Each migration runs in its own transaction under an advisory lock, and is recorded in
production.schema_migrations, so running this twice, or twice at once, applies each file once.
Migrations are never edited once merged: a change is a new file.

    agentic-ops-migrate --dsn postgresql://user@host/agentic_ops   (or AGENTIC_OPS_DSN)
"""

import argparse
import asyncio
import os
import re
import sys
from importlib.resources import files

import psycopg

SCHEMA = "production"
# pg_advisory_xact_lock key: any constant, the same for every runner
LOCK_KEY = 0x6167656E74696300  # "agentic\0"
FILENAME = re.compile(r"(\d{4})_[a-z0-9_]+\.sql")


def migrations() -> list[tuple[int, str, str]]:
    """(version, name, SQL) of every migration, in version order."""
    found = []
    for entry in files("agentic_ops_worker").joinpath("migrations").iterdir():
        m = FILENAME.fullmatch(entry.name)
        if m:
            found.append((int(m[1]), entry.name, entry.read_text()))
    found.sort()
    versions = [v for v, _, _ in found]
    if len(set(versions)) != len(versions):
        raise RuntimeError(f"two migrations share a version: {versions}")
    return found


async def migrate(conn: psycopg.AsyncConnection) -> list[str]:
    """Apply pending migrations; return the names of those applied."""
    applied = []
    for version, name, sql in migrations():
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            await conn.execute(
                f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};"
                f" CREATE TABLE IF NOT EXISTS {SCHEMA}.schema_migrations ("
                " version integer PRIMARY KEY, name text NOT NULL,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            )
            cur = await conn.execute(f"SELECT 1 FROM {SCHEMA}.schema_migrations WHERE version = %s", (version,))
            if await cur.fetchone():
                continue
            # No parameters, so the file may hold many statements
            await conn.execute(sql.encode())
            await conn.execute(
                f"INSERT INTO {SCHEMA}.schema_migrations (version, name) VALUES (%s, %s)", (version, name)
            )
            applied.append(name)
    return applied


async def _main(dsn: str) -> None:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        applied = await migrate(conn)
    print("\n".join(f"applied {name}" for name in applied) or "up to date")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", default=os.environ.get("AGENTIC_OPS_DSN"), help="default: $AGENTIC_OPS_DSN")
    args = parser.parse_args()
    if not args.dsn:
        parser.error("no --dsn and no AGENTIC_OPS_DSN")
    try:
        asyncio.run(_main(args.dsn))
    except psycopg.Error as e:
        sys.exit(f"migration failed: {e}")


if __name__ == "__main__":
    main()
