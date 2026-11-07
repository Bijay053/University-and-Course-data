"""Read-only release prerequisite for the shared document access registry."""
from sqlalchemy import text

REQUIRED = {
    "origin": ("text", False), "url": ("text", False),
    "policy": ("jsonb", False), "observation": ("jsonb", False),
    "history": ("jsonb", False),
    "last_success_at": ("timestamp with time zone", True),
    "updated_at": ("timestamp with time zone", False),
}


async def require_shape(db, table, required, primary_key):
    rows = (await db.execute(text("""
        SELECT a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type,
               NOT a.attnotnull AS nullable
        FROM pg_attribute a
        WHERE a.attrelid=to_regclass(:table)
          AND a.attnum>0 AND NOT a.attisdropped
    """), {"table": table})).mappings().all()
    actual = {r["name"]: (r["type"], r["nullable"]) for r in rows}
    primary = (await db.execute(text("""
        SELECT array_agg(a.attname ORDER BY k.position)
        FROM pg_constraint c
        CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, position)
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum
        WHERE c.conrelid=to_regclass(:table) AND c.contype='p'
    """), {"table": table})).scalar()
    if any(actual.get(k) != v for k, v in required.items()) or primary != primary_key:
        raise RuntimeError("Document storage prerequisites are absent or incompatible; apply reviewed migrations 394 and 395 before release.")


async def require_source_access_schema(db):
    await require_shape(db, "university_source_access", REQUIRED, ["origin", "url"])


async def require_document_budget_schema(db):
    await require_shape(db, "scrape_agent_spend", {
        "id": ("text", False), "day": ("date", False),
        "thread_id": ("text", False), "model": ("text", False),
        "reserved_usd": ("numeric(18,8)", False),
        "charged_usd": ("numeric(18,8)", False),
        "status": ("text", False), "created_at": ("timestamp with time zone", False),
    }, ["id"])


async def main():
    from app.database import AsyncSessionLocal, engine
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            await require_source_access_schema(db)
            await require_document_budget_schema(db)
            await db.rollback()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    import asyncio
    import sys
    try:
        asyncio.run(main())
    except Exception:
        print("Release refused: document schema prerequisites failed; review migrations 394 and 395.", file=sys.stderr)
        raise SystemExit(1)
    print("Source access registry and dollar budget prerequisites verified (read-only).")
