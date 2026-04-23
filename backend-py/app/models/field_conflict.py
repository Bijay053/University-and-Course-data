from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class FieldConflict(Base):
    __tablename__ = "field_conflicts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scraped_course_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("scraped_courses.id", ondelete="CASCADE")
    )
    course_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("courses.id", ondelete="CASCADE")
    )
    field_key: Mapped[str] = mapped_column(Text, nullable=False)
    value_a: Mapped[str | None] = mapped_column(Text)
    value_b: Mapped[str | None] = mapped_column(Text)
    evidence_a_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("scraped_field_evidence.id", ondelete="SET NULL")
    )
    evidence_b_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("scraped_field_evidence.id", ondelete="SET NULL")
    )
    conflict_type: Mapped[str] = mapped_column(Text, nullable=False, default="mismatch")
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    resolution: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
