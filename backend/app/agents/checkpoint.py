"""Where run setup threads are checkpointed: Postgres when DATABASE_URL is set, memory otherwise."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Checkpointer
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.settings import Settings


@asynccontextmanager
async def open_checkpointer(settings: Settings) -> AsyncIterator[Checkpointer]:
    if settings.database_url is None:
        yield InMemorySaver()
        return
    pool = AsyncConnectionPool(
        settings.database_url.get_secret_value(),
        max_size=10,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await pool.open()
    try:
        saver = AsyncPostgresSaver(pool)
        await saver.setup()  # creates or migrates the checkpoint tables
        yield saver
    finally:
        await pool.close()
