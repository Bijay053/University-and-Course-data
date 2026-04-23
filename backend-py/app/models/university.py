from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class University(Base):
    __tablename__ = "universities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(Text, nullable=False)
    website: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    logo_url: Mapped[str | None] = mapped_column(Text)
    scrape_url: Mapped[str | None] = mapped_column(Text)
    fee_page_url: Mapped[str | None] = mapped_column(Text)
    requirements_page_url: Mapped[str | None] = mapped_column(Text)
    scholarship_page_url: Mapped[str | None] = mapped_column(Text)
    academic_requirements_page_url: Mapped[str | None] = mapped_column(Text)
    scrape_config: Mapped[dict | None] = mapped_column(JSONB)
    featured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    featured_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    courses: Mapped[list["Course"]] = relationship(  # type: ignore[name-defined]  # noqa: F821
        back_populates="university", cascade="all, delete-orphan"
    )
