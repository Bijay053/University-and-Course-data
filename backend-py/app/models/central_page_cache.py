"""SQLAlchemy model for the central_page_cache table.

Stores the parsed output of central fee and English-requirements pages on a
per-university, per-page-type basis.  A cached entry is considered valid until
``expires_at``; after that the caller re-fetches and overwrites.

Manual cache invalidation can be triggered via the admin API
(``DELETE /api/universities/{id}/central-cache``).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Integer, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CentralPageCache(Base):
    __tablename__ = "central_page_cache"

    university_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_type: Mapped[str] = mapped_column(Text, primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    raw_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
