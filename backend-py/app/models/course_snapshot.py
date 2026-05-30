from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CourseSnapshot(Base):
    """Phase 10: immutable snapshot of a staged course's key fields after each scrape.

    One row per (scrape_job_id, course_name) pair. Never updated — new scrape
    produces a new set of rows. The change detector compares the latest job's
    rows against the previous job's rows for the same university.
    """

    __tablename__ = "course_snapshots"
    __table_args__ = (
        Index("ix_course_snapshots_uni_job", "university_id", "scrape_job_id"),
        Index("ix_course_snapshots_job", "scrape_job_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    university_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("universities.id", ondelete="CASCADE"), nullable=False
    )
    scrape_job_id: Mapped[str] = mapped_column(Text, nullable=False)
    course_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("courses.id", ondelete="SET NULL")
    )
    course_name: Mapped[str] = mapped_column(Text, nullable=False)
    course_url: Mapped[str | None] = mapped_column(Text)
    international_fee: Mapped[float | None] = mapped_column(Float)
    fee_term: Mapped[str | None] = mapped_column(Text)
    duration: Mapped[float | None] = mapped_column(Float)
    duration_term: Mapped[str | None] = mapped_column(Text)
    intake_months: Mapped[list | None] = mapped_column(JSONB)
    ielts_overall: Mapped[float | None] = mapped_column(Float)
    pte_overall: Mapped[float | None] = mapped_column(Float)
    toefl_overall: Mapped[float | None] = mapped_column(Float)
    academic_score: Mapped[float | None] = mapped_column(Float)
    academic_level: Mapped[str | None] = mapped_column(Text)
    other_requirement: Mapped[str | None] = mapped_column(Text)
    course_location: Mapped[str | None] = mapped_column(Text)
    study_mode: Mapped[str | None] = mapped_column(Text)
    degree_level: Mapped[str | None] = mapped_column(Text)
    avg_verification_confidence: Mapped[float | None] = mapped_column(Float)
    auto_publish_status: Mapped[str | None] = mapped_column(Text)
    page_hash: Mapped[str | None] = mapped_column(Text)
    snapshotted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
