"""Apply only task644 DDL to verified local PostgreSQL; never stamp divergent history."""
import asyncio
import importlib.util
import ipaddress
import os
from pathlib import Path
import socket

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from app.database import engine
from app.schema_readiness import require_qualification_guidance_schema


async def main():
    url = engine.url
    addresses = {r[4][0] for r in socket.getaddrinfo(url.host, url.port or 5432)}
    loopback = addresses and all(ipaddress.ip_address(a).is_loopback for a in addresses)
    replit_local = (url.host == "helium" and bool(os.environ.get("REPL_ID"))
                    and addresses and all(ipaddress.ip_address(a).is_private for a in addresses))
    if url.get_backend_name() != "postgresql" or not (loopback or replit_local):
        raise SystemExit("Refusing migration: target is not verified local PostgreSQL")
    print("Target verified: local development PostgreSQL (no remote target allowed)")
    path = Path(__file__).resolve().parents[1] / "alembic/versions/390_qualification_approval_guidance.py"
    spec = importlib.util.spec_from_file_location("migration_390", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with engine.begin() as connection:
        exists = (await connection.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() "
            "AND table_name='scraped_courses' AND column_name='last_qualification_approval'"
        ))).scalar()

        def upgrade(sync_connection):
            migration.op = Operations(MigrationContext.configure(sync_connection))
            if exists:
                migration.install_invalidation_trigger()
            else:
                migration.upgrade()

        await connection.run_sync(upgrade)
        await require_qualification_guidance_schema(connection)
        # Do not run any preceding migrations or stamp an unrelated revision.
        await connection.execute(text(
            "UPDATE alembic_version SET version_num='390_qualification_guidance' "
            "WHERE version_num='389_course_identity_audit'"
        ))
    await engine.dispose()
    print("Migration 390 column and invalidation trigger ready; no unrelated migrations run")


if __name__ == "__main__":
    asyncio.run(main())