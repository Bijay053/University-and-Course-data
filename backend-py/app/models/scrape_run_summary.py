"""ORM model for ``scrape_run_summary`` — Week 2 P1 wide-format metrics table.

One row per completed scrape run. Companion to (not replacement for) the
existing long-format ``scrape_run_metrics`` table.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Computed, DateTime, ForeignKey, Integer, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ScrapeRunSummary(Base):
    __tablename__ = "scrape_run_summary"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scrape_run_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("scrape_runtime_jobs.runtime_job_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    university_id: Mapped[int] = mapped_column(Integer, nullable=False)

    run_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # run_duration_seconds is a STORED generated column — read-only from Python.
    run_duration_seconds: Mapped[int | None] = mapped_column(
        Integer,
        Computed(
            "EXTRACT(EPOCH FROM (run_finished_at - run_started_at))::int",
            persisted=True,
        ),
        nullable=True,
    )

    # Discovery
    candidates_discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidates_staged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidates_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Skip-reason breakdown
    skipped_domestic_only: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_online_only: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_no_international_fee: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_category_landing_page: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_generic_category_page: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_fetch_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_other: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Per-field fill rates
    fill_rate_international_fee: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    fill_rate_ielts_overall: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    fill_rate_pte_overall: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    fill_rate_toefl_overall: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    fill_rate_duration: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    fill_rate_intake_months: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    fill_rate_course_location: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    fill_rate_study_mode: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    fill_rate_cricos_code: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)

    # Method distribution
    method_distribution: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Cost
    gemini_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gemini_cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False, default=0)
    # avg_cost_per_course is a STORED generated column.
    avg_cost_per_course: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 6),
        Computed(
            "CASE WHEN candidates_staged > 0 "
            "THEN gemini_cost_usd / candidates_staged ELSE 0 END",
            persisted=True,
        ),
        nullable=True,
    )

    # Errors
    fetch_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
