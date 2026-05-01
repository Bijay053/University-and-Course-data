from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DiscoveryFailureAlert(Base):
    """Persisted when all discovery tiers complete and candidates < 3.

    Surfaces silent zero/near-zero discovery failures loudly in the admin
    UI instead of burying them in logs.  ``resolved_at`` is set manually
    by an operator (or by a follow-up successful scrape) once the root
    cause is understood.
    """

    __tablename__ = "discovery_failure_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    university_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("universities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    candidates_found: Mapped[int] = mapped_column(Integer, nullable=False)
    diagnostic: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by: Mapped[str | None] = mapped_column(Text, nullable=True)
