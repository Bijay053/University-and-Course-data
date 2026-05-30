"""ORM model — university_watchers (Phase 13: Autonomous Monitoring Engine)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UniversityWatcher(Base):
    __tablename__ = "university_watchers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    university_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("universities.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    monitoring_strategy: Mapped[str] = mapped_column(Text, nullable=False, default="passive")
    probe_url: Mapped[str | None] = mapped_column(Text)
    etag: Mapped[str | None] = mapped_column(Text)
    page_hash: Mapped[str | None] = mapped_column(Text)
    sitemap_hash: Mapped[str | None] = mapped_column(Text)
    last_probe_result: Mapped[str | None] = mapped_column(Text)
    last_probe_status_code: Mapped[int | None] = mapped_column(Integer)
    last_probe_error: Mapped[str | None] = mapped_column(Text)
    consecutive_unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_checks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_changes_detected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_scrapes_triggered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    change_frequency_days: Mapped[float | None] = mapped_column(Float)
    most_changed_pages: Mapped[list | None] = mapped_column(JSONB)
    most_stable_pages: Mapped[list | None] = mapped_column(JSONB)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_scrape_job_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("scrape_runtime_jobs.runtime_job_id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
