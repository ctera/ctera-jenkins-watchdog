from __future__ import annotations

import asyncio
import os

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text

from alembic import command
from jenkins_watchdog.infrastructure.database import create_engine
from jenkins_watchdog.infrastructure.models import Base


def alembic_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


async def reset_and_inspect(database_url: str, *, reset: bool) -> tuple[set[str], str | None]:
    engine = create_engine(database_url)
    async with engine.begin() as connection:
        if reset:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        tables = set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version")) if not reset else None
    await engine.dispose()
    return tables, revision


def test_migration_from_empty_database_reaches_single_head_without_drift() -> None:
    database_url = os.environ.get("WATCHDOG_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("WATCHDOG_TEST_DATABASE_URL is required for PostgreSQL migration tests")
    asyncio.run(reset_and_inspect(database_url, reset=True))
    config = alembic_config(database_url)

    command.upgrade(config, "head")
    command.check(config)
    tables, revision = asyncio.run(reset_and_inspect(database_url, reset=False))

    assert tables == set(Base.metadata.tables) | {"alembic_version"}
    assert revision == "0001_v2_schema"
