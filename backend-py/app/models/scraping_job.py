from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ScrapingJob(Base):
    __tablename__ = "scraping_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    university_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("universities.id", ondelete="SET NULL")
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    frequency: Mapped[str] = mapped_column(Text, nullable=False, default="weekly")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    last_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
