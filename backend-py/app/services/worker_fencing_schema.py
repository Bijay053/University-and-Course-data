"""Read-only prerequisites shared by repair admission and release preflight.

This file can also be streamed from a reviewed Git target into Python before
checkout. Its CLI uses the current deployment's configured database connection;
it never applies migrations, stamps Alembic, or modifies ownership rows.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


REQUIRED_COLUMNS = {
    "autonomous_worker_claims": {
        "claim_key": "text", "generation": "text", "state": "text",
        "task_id": "text", "process_identity": "text", "proof": "text",
        "claimed_at": "timestamp with time zone", "revoked_generations": "jsonb",
    },
    "autonomous_worker_budgets": {
        "claim_key": "text", "live_pages": "integer", "live_seconds": "double precision",
    },
}


class WorkerFencingPrerequisiteError(RuntimeError):
    code = "worker_fencing_schema_unavailable"


class WorkerFencingSchemaMissing(WorkerFencingPrerequisiteError):
    code = "worker_fencing_schema_missing"


async def require_worker_fencing_schema(db) -> None:
    """Inspect the relations resolved by this connection's actual search_path."""
    try:
        rows = (await db.execute(text("""
            SELECT c.relname AS relation_name, a.attname AS column_name,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS column_type
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
            WHERE c.oid IN (to_regclass('autonomous_worker_claims'),
                            to_regclass('autonomous_worker_budgets'))
              AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
        """))).mappings().all()
    except SQLAlchemyError as exc:
        raise WorkerFencingPrerequisiteError(
            "Cannot verify the repair worker database prerequisites. "
            "An administrator must check database availability and permissions; "
            "no repair was started."
        ) from exc
    actual = {
        (row["relation_name"], row["column_name"]): row["column_type"] for row in rows
    }
    missing = [
        f"{table}.{column}"
        for table, columns in REQUIRED_COLUMNS.items()
        for column, kind in columns.items()
        if actual.get((table, column)) != kind
    ]
    if missing:
        raise WorkerFencingSchemaMissing(
            "Repair is unavailable: required worker-fencing database schema is missing "
            "or incompatible (" + ", ".join(missing) + "). "
            "An administrator must review and install the specific prerequisites from "
            "382_worker_claims and 383_claim_lock_lineage using an approved, scoped "
            "migration plan. Do not blindly upgrade or stamp unrelated migrations."
        )


async def _preflight() -> None:
    from app.database import AsyncSessionLocal, engine
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            await require_worker_fencing_schema(db)
            await db.rollback()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    import asyncio
    import sys
    try:
        asyncio.run(_preflight())
    except WorkerFencingPrerequisiteError as exc:
        print(f"Release refused: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        # Never leak a connection string or credentials through release output.
        print("Release refused: worker-fencing database prerequisites could not be checked.", file=sys.stderr)
        raise SystemExit(1)
    print("Worker-fencing schema prerequisites verified (read-only).")