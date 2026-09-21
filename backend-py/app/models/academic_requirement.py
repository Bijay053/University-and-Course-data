from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AcademicRequirement(Base):
    __tablename__ = "academic_requirements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    academic_level: Mapped[str | None] = mapped_column(Text)
    academic_level_option_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("academic_level_options.id", ondelete="RESTRICT"),
        nullable=True, index=True,
    )
    academic_level_option: Mapped["AcademicLevelOption | None"] = relationship(
        "AcademicLevelOption", lazy="joined"
    )
    academic_score: Mapped[float | None] = mapped_column(Float)
    score_type: Mapped[str | None] = mapped_column(Text)
    academic_country: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
