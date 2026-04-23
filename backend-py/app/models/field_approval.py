from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CourseFieldApproval(Base):
    __tablename__ = "course_field_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False
    )
    field_key: Mapped[str] = mapped_column(Text, nullable=False)
    final_value: Mapped[str | None] = mapped_column(Text)
    source_evidence_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("scraped_field_evidence.id", ondelete="SET NULL")
    )
    decision_score: Mapped[float | None] = mapped_column(Float)
    approval_status: Mapped[str] = mapped_column(Text, nullable=False, default="approved")
    approved_by: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
