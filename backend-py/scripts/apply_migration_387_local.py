"""Apply only additive offering DDL to verified local Replit PostgreSQL.

Does not advance/stamp a divergent Alembic revision or modify course records.
"""
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


async def main():
    url = engine.url
    addresses = {r[4][0] for r in socket.getaddrinfo(url.host, url.port or 5432)}
    loopback = addresses and all(ipaddress.ip_address(a).is_loopback for a in addresses)
    replit_local = (
        url.host == "helium" and bool(os.environ.get("REPL_ID"))
        and addresses and all(ipaddress.ip_address(a).is_private for a in addresses)
    )
    if url.get_backend_name() != "postgresql" or not (loopback or replit_local):
        raise SystemExit("Refusing migration: target is not verified local PostgreSQL")
    print("Target verified: " + ("loopback PostgreSQL" if loopback else "local Replit PostgreSQL service"))
    async with engine.begin() as conn:
        revisions = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
        print("Existing revision tracking: " + ", ".join(revisions))
        table = (await conn.execute(text("SELECT to_regclass('public.course_offerings')"))).scalar()
        column = (await conn.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
            "AND table_name='courses' AND column_name='offering_identity'"
        ))).scalar()
        if bool(table) != bool(column):
            raise RuntimeError("Partial offering schema detected; explicit inspection required")
        if not table:
            path = Path(__file__).resolve().parents[1] / "alembic/versions/387_course_offerings.py"
            spec = importlib.util.spec_from_file_location("migration_387", path)
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            def upgrade(connection):
                migration.op = Operations(MigrationContext.configure(connection))
                migration.upgrade()
            await conn.run_sync(upgrade)
            print("Applied revision 387 DDL only")
        else:
            print("Offering schema already exists; no DDL applied")
        await conn.execute(text(
            "SELECT id, course_id, location_key, location, fee_amount, fee_currency, "
            "fee_term, fee_year, source_url FROM course_offerings LIMIT 0"
        ))
        constraints = (await conn.execute(text(
            "SELECT count(*) FROM pg_constraint WHERE conname IN "
            "('uq_courses_offering_identity', 'uq_course_offering_location') "
            "AND connamespace = 'public'::regnamespace AND contype = 'u'"
        ))).scalar_one()
        if constraints != 2:
            raise RuntimeError("Required offering uniqueness constraints are missing")
        if revisions == ["386_campus_fee_scope"]:
            await conn.execute(text(
                "UPDATE alembic_version SET version_num='387_course_offerings' "
                "WHERE version_num='386_campus_fee_scope'"
            ))
            print("Advanced exact parent revision 386 to verified revision 387")
        else:
            print("Revision tracking unchanged; no full-head migration or stamp")
        count = (await conn.execute(text("SELECT count(*) FROM course_offerings"))).scalar_one()
        print("Persisted offering records: " + str(count))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())