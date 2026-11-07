"""Read-only release prerequisite for the shared document access registry."""
from sqlalchemy import text

REQUIRED = {
    "origin": ("text", False), "url": ("text", False),
    "policy": ("jsonb", False), "observation": ("jsonb", False),
    "history": ("jsonb", False),
    "last_success_at": ("timestamp with time zone", True),
    "updated_at": ("timestamp with time zone", False),
}


async def require_source_access_schema(db):
    rows = (await db.execute(text("""
        SELECT a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type,
               NOT a.attnotnull AS nullable
        FROM pg_attribute a
        WHERE a.attrelid=to_regclass('university_source_access')
          AND a.attnum>0 AND NOT a.attisdropped
    """))).mappings().all()
    actual = {r["name"]: (r["type"], r["nullable"]) for r in rows}
    primary = (await db.execute(text("""
        SELECT array_agg(a.attname ORDER BY k.position)
        FROM pg_constraint c
        CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, position)
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum
        WHERE c.conrelid=to_regclass('university_source_access') AND c.contype='p'
    """))).scalar()
    if any(actual.get(k) != v for k, v in REQUIRED.items()) or primary != ["origin", "url"]:
        raise RuntimeError("Source access registry schema is absent or incompatible; apply reviewed migration 394 before release.")


async def main():
    from app.database import AsyncSessionLocal, engine
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            await require_source_access_schema(db)
            await db.rollback()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    import asyncio
    import sys
    try:
        asyncio.run(main())
    except Exception:
        print("Release refused: source access schema prerequisites failed; review migration 394.", file=sys.stderr)
        raise SystemExit(1)
    print("Source access registry prerequisites verified (read-only).")
