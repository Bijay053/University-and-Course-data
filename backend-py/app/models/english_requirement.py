from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EnglishRequirement(Base):
    __tablename__ = "english_requirements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False
    )
    test_type: Mapped[str] = mapped_column(Text, nullable=False)
    test_name: Mapped[str | None] = mapped_column(Text)
    listening: Mapped[float | None] = mapped_column(Float)
    speaking: Mapped[float | None] = mapped_column(Float)
    writing: Mapped[float | None] = mapped_column(Float)
    reading: Mapped[float | None] = mapped_column(Float)
    overall: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
