from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, Numeric, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UniversityFieldBaseline(Base):
    __tablename__ = "university_field_baselines"
    __table_args__ = (
        UniqueConstraint("university_id", "field_key", name="uq_baseline_uni_field"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    university_id: Mapped[int] = mapped_column(Integer, nullable=False)
    field_key: Mapped[str] = mapped_column(Text, nullable=False)
    expected_fill_rate: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    expected_method_distribution: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    floor_threshold: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
