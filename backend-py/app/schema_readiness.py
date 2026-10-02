"""Fail closed before serving work; this check never runs schema DDL."""
from sqlalchemy import text

ERROR = "Qualification guidance schema is not ready; apply migration 390 before starting API or workers."


async def require_qualification_guidance_schema(connection):
    ready = (await connection.execute(text("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = 'scraped_courses'
              AND column_name = 'last_qualification_approval' AND udt_name = 'jsonb'
        ) AND EXISTS (
            SELECT 1 FROM pg_trigger
            WHERE tgrelid = to_regclass('scraped_courses')
              AND tgname = 'clear_changed_qualification_guidance'
              AND tgenabled IN ('O', 'A') AND NOT tgisinternal
        )
    """))).scalar_one()
    if not ready:
        raise RuntimeError(ERROR)


async def check_worker_schema():
    # Dedicated loop-local engine: never leave asyncpg pooled connections tied
    # to the startup asyncio.run loop for later Celery task loops.
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool
    from app.config import settings
    from app.database import postgres_tls_connect_args
    engine = create_async_engine(settings.database_url, poolclass=NullPool,
                                 connect_args=postgres_tls_connect_args())
    from app.tasks.loop_resources import owned_engine
    async with owned_engine(engine):
        async with engine.connect() as connection:
            await require_qualification_guidance_schema(connection)