"""Async SQLAlchemy engine and session factory.

The engine is created once at import time. ``get_db`` is a FastAPI dependency
that yields a session and ensures it's always closed.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
import ssl

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Project-wide declarative base."""


def postgres_tls_connect_args() -> dict[str, ssl.SSLContext]:
    """Return certificate-verifying TLS options for managed production.

    URL query flags such as ``sslmode`` are libpq-specific and are deliberately
    removed by config normalization. The root-owned production database
    environment sets ``DATABASE_REQUIRE_TLS=true``; local Replit PostgreSQL
    rejects SSL upgrades and therefore keeps the development default disabled.
    """
    if not settings.database_require_tls:
        return {}
    return {"ssl": ssl.create_default_context()}


engine = create_async_engine(
    settings.database_url,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
    echo=settings.debug,
    future=True,
    connect_args=postgres_tls_connect_args(),
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
