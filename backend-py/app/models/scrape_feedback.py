from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ScrapeFeedback(Base):
    __tablename__ = "scrape_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    university_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("universities.id", ondelete="CASCADE")
    )
    scraped_course_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("scraped_courses.id", ondelete="SET NULL")
    )
    course_name: Mapped[str | None] = mapped_column(Text)
    field_key: Mapped[str | None] = mapped_column(Text)
    issue_type: Mapped[str] = mapped_column(Text, nullable=False, default="generic")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    preferred_value: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
