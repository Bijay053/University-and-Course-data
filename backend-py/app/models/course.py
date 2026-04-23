from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    university_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("universities.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(Text)
    sub_category: Mapped[str | None] = mapped_column(Text)
    course_website: Mapped[str | None] = mapped_column(Text)
    duration: Mapped[float | None] = mapped_column(Float)
    duration_term: Mapped[str | None] = mapped_column(Text)
    study_mode: Mapped[str | None] = mapped_column(Text)
    degree_level: Mapped[str | None] = mapped_column(Text)
    study_load: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    course_structure: Mapped[str | None] = mapped_column(Text)
    career_outcomes: Mapped[str | None] = mapped_column(Text)
    other_test: Mapped[str | None] = mapped_column(Text)
    other_test_score: Mapped[str | None] = mapped_column(Text)
    other_requirement: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    course_location: Mapped[str | None] = mapped_column(Text)
    student_market: Mapped[str | None] = mapped_column(Text)
    delivery_mode: Mapped[str | None] = mapped_column(Text)
    international_eligible: Mapped[bool | None] = mapped_column(Boolean)
    on_campus_available: Mapped[bool | None] = mapped_column(Boolean)
    eligibility_status: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    eligibility_reason: Mapped[str | None] = mapped_column(Text)
    eligibility_confidence: Mapped[float | None] = mapped_column(Float)
    approval_status: Mapped[str] = mapped_column(Text, nullable=False, default="approved")
    approval_score: Mapped[float | None] = mapped_column(Float)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_edited_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    university = relationship("University", back_populates="courses")
