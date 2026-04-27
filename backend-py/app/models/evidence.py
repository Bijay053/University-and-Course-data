from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ScrapedFieldEvidence(Base):
    __tablename__ = "scraped_field_evidence"
    __table_args__ = (
        UniqueConstraint(
            "scraped_course_id", "field_key", "extraction_method", "source_url",
            name="scraped_field_evidence_dedup",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scraped_course_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("scraped_courses.id", ondelete="CASCADE"), nullable=False
    )
    field_key: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_value: Mapped[str | None] = mapped_column(Text)
    normalized_value: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    page_type: Mapped[str | None] = mapped_column(Text)
    extraction_method: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    decision_score: Mapped[float | None] = mapped_column(Float)
    validation_status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    decision_status: Mapped[str] = mapped_column(Text, nullable=False, default="needs_review")
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
