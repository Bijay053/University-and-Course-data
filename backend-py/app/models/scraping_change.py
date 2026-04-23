from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ScrapingChange(Base):
    __tablename__ = "scraping_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scraping_job_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("scraping_jobs.id", ondelete="SET NULL")
    )
    scraped_course_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("scraped_courses.id", ondelete="SET NULL")
    )
    course_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("courses.id", ondelete="SET NULL")
    )
    university_name: Mapped[str | None] = mapped_column(Text)
    course_name: Mapped[str | None] = mapped_column(Text)
    field_changed: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    reason: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
