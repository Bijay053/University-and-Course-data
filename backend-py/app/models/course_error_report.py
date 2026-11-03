"""Visitor reports are review-only data, never scraper instructions."""
from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class CourseErrorReport(Base):
    __tablename__ = "course_error_reports"
    __table_args__ = (
        CheckConstraint("status IN ('new', 'reviewed', 'resolved')", name="ck_course_error_report_status"),
        CheckConstraint("kind IN ('missing', 'incorrect')", name="ck_course_error_report_kind"),
        Index("ix_course_error_reports_status_created", "status", "created_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id", ondelete="SET NULL"))
    course_name: Mapped[str | None] = mapped_column(Text)
    university_id: Mapped[int | None] = mapped_column(ForeignKey("universities.id", ondelete="SET NULL"))
    insight_id: Mapped[int | None] = mapped_column(ForeignKey("assessment_notes.id", ondelete="SET NULL"))
    insight_country: Mapped[str | None] = mapped_column(Text)
    university_name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    field: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="new", default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
