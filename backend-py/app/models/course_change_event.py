from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CourseChangeEvent(Base):
    """Phase 10: a single detected change between two consecutive scrape snapshots.

    change_type: new_course | removed_course | field_change
    severity:    critical | major | minor | info
    status:      new | acknowledged | resolved
    """

    __tablename__ = "course_change_events"
    __table_args__ = (
        Index("ix_cce_uni_severity", "university_id", "severity"),
        Index("ix_cce_job", "scrape_job_id"),
        Index("ix_cce_uni_detected", "university_id", "detected_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    university_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("universities.id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("courses.id", ondelete="SET NULL")
    )
    course_name: Mapped[str] = mapped_column(Text, nullable=False)
    scrape_job_id: Mapped[str] = mapped_column(Text, nullable=False)
    field_name: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    change_type: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_before: Mapped[float | None] = mapped_column(Float)
    confidence_after: Mapped[float | None] = mapped_column(Float)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="new")
