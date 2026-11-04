"""Read-only reporting prerequisites, executable from immutable target Git bytes."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


# Types and nullability required by the course and Key Insights report model.
REQUIRED_COLUMNS = {
    "id": ("integer", False),
    "course_id": ("integer", True),
    "course_name": ("text", True),
    "university_id": ("integer", True),
    "insight_id": ("integer", True),
    "insight_country": ("text", True),
    "university_name": ("text", False),
    "kind": ("text", False),
    "field": ("text", False),
    "message": ("text", False),
    "source_url": ("text", True),
    "status": ("text", False),
    "created_at": ("timestamp with time zone", False),
    "updated_at": ("timestamp with time zone", False),
}


class ReportingPrerequisiteError(RuntimeError):
    pass


async def require_reporting_schema(db) -> None:
    """Check actual search-path resolution, not just the migration ledger."""
    try:
        rows = (await db.execute(text("""
            SELECT a.attname AS column_name,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS column_type,
                   NOT a.attnotnull AS nullable
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
            WHERE c.oid = to_regclass('course_error_reports')
              AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
        """))).mappings().all()
    except SQLAlchemyError as exc:
        raise ReportingPrerequisiteError(
            "Reporting database prerequisites could not be checked. "
            "Check database availability and permissions."
        ) from exc
    actual = {
        row["column_name"]: (row["column_type"], row["nullable"]) for row in rows
    }
    incompatible = [
        f"course_error_reports.{name}"
        for name, expected in REQUIRED_COLUMNS.items()
        if actual.get(name) != expected
    ]
    if incompatible:
        raise ReportingPrerequisiteError(
            "Reporting schema is missing or incompatible (" + ", ".join(incompatible) + "). "
            "Review and apply the specific prerequisites from 391_public_course_reports "
            "and 392_insight_error_reports using an approved, scoped migration plan. "
            "Do not blindly upgrade or stamp unrelated migrations."
        )


async def _preflight() -> None:
    from app.database import AsyncSessionLocal, engine

    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            await require_reporting_schema(db)
            await db.rollback()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    import asyncio
    import sys

    try:
        asyncio.run(_preflight())
    except ReportingPrerequisiteError as exc:
        print(f"Release refused: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        # Connection errors must never print credentials or a connection string.
        print("Release refused: reporting database prerequisites could not be checked.", file=sys.stderr)
        raise SystemExit(1)
    print("Reporting schema prerequisites verified (read-only).")
