"""ORM model for the gemini_call_log table (Component 4).

Each row records one call to the Gemini API — text or vision — with full
cost, token, and success metadata so cost-reporting SQL views can be built
on top.

The ``scrape_run_id`` column is a TEXT FK matching
``scrape_runtime_jobs.runtime_job_id`` (TEXT primary key).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GeminiCallLog(Base):
    __tablename__ = "gemini_call_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scrape_run_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("scrape_runtime_jobs.runtime_job_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    university_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("universities.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    course_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_type: Mapped[str] = mapped_column(Text, nullable=False, default="primary_full")
    model: Mapped[str] = mapped_column(Text, nullable=False, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
