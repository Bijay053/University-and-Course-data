from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class FieldVerificationResult(Base):
    """Phase 9 — per-field cross-source verification outcome for a staged course."""

    __tablename__ = "field_verification_results"
    __table_args__ = (
        UniqueConstraint(
            "scraped_course_id", "field_name",
            name="fvr_course_field_uniq",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scraped_course_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("scraped_courses.id", ondelete="CASCADE"), nullable=False
    )
    field_name: Mapped[str] = mapped_column(Text, nullable=False)
    verified_value: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="needs_review")
    source_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sources: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    conflict_sources: Mapped[list | None] = mapped_column(JSONB)
    verification_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
