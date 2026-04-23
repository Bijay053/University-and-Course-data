from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CourseAcronymOption(Base):
    __tablename__ = "course_acronym_options"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    acronym: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
