from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CourseAuditLog(Base):
    __tablename__ = "course_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("courses.id", ondelete="CASCADE")
    )
    scraped_course_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("scraped_courses.id", ondelete="SET NULL")
    )
    source_evidence_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("scraped_field_evidence.id", ondelete="SET NULL")
    )
    field_key: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
