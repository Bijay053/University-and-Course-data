"""Secure connection setup for narrow legacy repair entrypoints.

These helpers do not run Alembic, execute DDL or advance migration history.
Prefer the supported Alembic launcher for normal upgrades.
"""
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.managed_database_environment import load_managed_database_environment


def create_migration_engine(**options):
    """Use fresh managed settings, never a caller-supplied URL or TLS override."""
    if {"url", "connect_args"} & options.keys():
        raise ValueError("Legacy migrations must use managed connection settings")
    load_managed_database_environment()
    # Ordering matters: managed environment must precede settings/database imports.
    from app.config import Settings
    from app.database import postgres_tls_connect_args
    from sqlalchemy.ext.asyncio import create_async_engine

    settings = Settings()
    return create_async_engine(
        settings.database_url,
        connect_args=postgres_tls_connect_args(settings),
        **options,
    )


engine = create_migration_engine(echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
from app.database import Base  # noqa: E402 -- loaded only after managed configuration
