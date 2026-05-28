"""CourseSubCategory — persistent, auto-growing sub-category vocabulary.

Each row is a (category, sub_category) pair that is valid for course
classification.  The table is pre-seeded with the hard-coded options
from the frontend ``course-constants.ts`` file and grows automatically
whenever ``approve_course.py`` encounters a new Gemini-generated
sub_category that does not fuzzy-match an existing option.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CourseSubCategory(Base):
    __tablename__ = "course_sub_categories"
    __table_args__ = (
        UniqueConstraint("category", "sub_category", name="uq_course_sub_cat"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    sub_category: Mapped[str] = mapped_column(Text, nullable=False)
    auto_added: Mapped[bool] = mapped_column(
        "auto_added",
        Boolean,
        nullable=False,
        default=False,
        doc="True when this row was inserted automatically from scrape data.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
