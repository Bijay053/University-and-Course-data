from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Double, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentRecoveryResult(Base):
    __tablename__ = "agent_recovery_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scraped_course_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("scraped_courses.id", ondelete="CASCADE"), nullable=False
    )
    scrape_run_id: Mapped[str] = mapped_column(Text, nullable=False)
    field: Mapped[str] = mapped_column(Text, nullable=False)
    recovered_value: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    evidence_text: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Double)
    mapping_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
