"""SQLAlchemy model for scrape_performance_ledger (Phase 8)."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, Float, Integer, String, Text, ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ScrapePerformanceLedger(Base):
    __tablename__ = "scrape_performance_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    runtime_job_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    university_id: Mapped[int] = mapped_column(Integer, nullable=False)
    university_name: Mapped[Optional[str]] = mapped_column(Text)
    recorded_at: Mapped[Optional[datetime]] = mapped_column()

    first_completeness: Mapped[Optional[float]] = mapped_column(Float)
    final_completeness: Mapped[Optional[float]] = mapped_column(Float)
    completeness_gain: Mapped[Optional[float]] = mapped_column(Float)
    crossed_85_threshold: Mapped[bool] = mapped_column(Boolean, default=False)

    courses_staged: Mapped[int] = mapped_column(Integer, default=0)
    courses_auto_published: Mapped[int] = mapped_column(Integer, default=0)

    cascade_fired: Mapped[bool] = mapped_column(Boolean, default=False)
    repair_extractor_fired: Mapped[bool] = mapped_column(Boolean, default=False)
    pdf_quality_gate_fired: Mapped[bool] = mapped_column(Boolean, default=False)
    browser_retry_fired: Mapped[bool] = mapped_column(Boolean, default=False)
    quality_optimizer_fired: Mapped[bool] = mapped_column(Boolean, default=False)
    human_intervention_needed: Mapped[bool] = mapped_column(Boolean, default=False)

    pct_html: Mapped[float] = mapped_column(Float, default=0.0)
    pct_api: Mapped[float] = mapped_column(Float, default=0.0)
    pct_pdf: Mapped[float] = mapped_column(Float, default=0.0)
    pct_ai_rules: Mapped[float] = mapped_column(Float, default=0.0)
    pct_gemini: Mapped[float] = mapped_column(Float, default=0.0)
    pct_pattern: Mapped[float] = mapped_column(Float, default=0.0)

    gemini_calls: Mapped[int] = mapped_column(Integer, default=0)
    gemini_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    patterns_reused: Mapped[int] = mapped_column(Integer, default=0)

    p7_inline_improved: Mapped[int] = mapped_column(Integer, default=0)
    p7_celery_dispatched: Mapped[List[str]] = mapped_column(
        ARRAY(Text), default=list
    )

    job_started_at: Mapped[Optional[datetime]] = mapped_column()
    job_completed_at: Mapped[Optional[datetime]] = mapped_column()
