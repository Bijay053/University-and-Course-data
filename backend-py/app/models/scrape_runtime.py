from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ScrapeRuntimeJob(Base):
    __tablename__ = "scrape_runtime_jobs"

    runtime_job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    scraping_job_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("scraping_jobs.id", ondelete="SET NULL")
    )
    university_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("universities.id", ondelete="SET NULL")
    )
    university_name: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    request_payload: Mapped[dict | None] = mapped_column(JSONB)
    discovered_config: Mapped[dict | None] = mapped_column(JSONB)
    approval_summary: Mapped[dict | None] = mapped_column(JSONB)
    approval_decision: Mapped[bool | None] = mapped_column(Boolean)
    stop_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fast_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    imported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    log_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worker_id: Mapped[str | None] = mapped_column(Text)
    worker_pid: Mapped[int | None] = mapped_column(Integer)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    claim_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requeue_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requeue_events: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    cost_ceiling_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    total_gemini_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ScrapeRuntimeLog(Base):
    __tablename__ = "scrape_runtime_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    runtime_job_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("scrape_runtime_jobs.runtime_job_id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
