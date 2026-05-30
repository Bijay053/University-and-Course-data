from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Float, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CountryPattern(Base):
    __tablename__ = "country_patterns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    country: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    common_platforms: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    common_fee_patterns: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    common_intake_patterns: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    common_requirement_patterns: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    common_pdf_patterns: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    preferred_strategy: Mapped[str] = mapped_column(Text, nullable=False, default="bfs")
    known_risks: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_completeness: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    last_scrape_at: Mapped[datetime | None] = mapped_column(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=datetime.utcnow)
