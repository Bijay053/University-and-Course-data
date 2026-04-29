from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ScrapeRunMetrics(Base):
    __tablename__ = "scrape_run_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scrape_run_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("scrape_runtime_jobs.runtime_job_id", ondelete="CASCADE"),
        nullable=False,
    )
    university_id: Mapped[int] = mapped_column(Integer, nullable=False)
    field_key: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    courses_total: Mapped[int] = mapped_column(Integer, nullable=False)
    fill_rate: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
