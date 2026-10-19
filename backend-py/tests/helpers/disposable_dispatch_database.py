"""Test-owned PostgreSQL tables for real, committed scrape dispatch requests."""
from contextlib import asynccontextmanager
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import Base, postgres_tls_connect_args
from app.models import ScrapeRuntimeJob, University


@asynccontextmanager
async def disposable_dispatch_database():
    """No public search-path fallback, application sessions, or shared pools.

    Callers must explicitly opt in on a development/test database. Separate
    connections may commit freely; teardown drops only this random schema.
    """
    schema = "test_dispatch_" + uuid.uuid4().hex
    admin = create_async_engine(
        settings.database_url, poolclass=NullPool,
        connect_args=postgres_tls_connect_args(),
    )
    engine = create_async_engine(
        settings.database_url, poolclass=NullPool,
        connect_args={
            **postgres_tls_connect_args(),
            "server_settings": {
                "search_path": schema,
                "statement_timeout": "10000",
                "lock_timeout": "8000",
            },
        },
    )
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        tables = {University.__table__, ScrapeRuntimeJob.__table__}
        while True:
            expanded = tables | {
                fk.column.table for table in tables for fk in table.foreign_keys
            }
            if expanded == tables:
                break
            tables = expanded
        async with engine.begin() as connection:
            assert (await connection.execute(text("SELECT current_schema()"))).scalar_one() == schema
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(sync, tables=list(tables))
            )
        yield engine
    finally:
        await engine.dispose()
        try:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                assert (await connection.execute(text(
                    "SELECT count(*) FROM pg_namespace WHERE nspname = :schema"
                ), {"schema": schema})).scalar_one() == 0
        finally:
            await admin.dispose()
