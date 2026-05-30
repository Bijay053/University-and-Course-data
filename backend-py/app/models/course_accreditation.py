from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CourseAccreditation(Base):
    """Phase 11: professional / regulatory accreditation for a course."""

    __tablename__ = "course_accreditations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    accrediting_body: Mapped[str] = mapped_column(Text, nullable=False)
    accreditation_type: Mapped[str | None] = mapped_column(Text)
    accreditation_url: Mapped[str | None] = mapped_column(Text)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[str | None] = mapped_column(Text)

    course = relationship("Course", foreign_keys=[course_id])
