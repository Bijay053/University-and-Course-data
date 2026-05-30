from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PublishingLedger(Base):
    __tablename__ = "publishing_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scraped_course_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("scraped_courses.id", ondelete="SET NULL"), nullable=True
    )
    university_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("universities.id", ondelete="CASCADE"), nullable=False
    )
    course_name: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    pub_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    pub_score_breakdown: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    actor: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
