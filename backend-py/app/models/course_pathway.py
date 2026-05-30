from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CoursePathway(Base):
    """Phase 11: articulation / prerequisite link between two courses.

    pathway_type: articulation | credit_transfer | prerequisite | co_requisite
    """

    __tablename__ = "course_pathways"
    __table_args__ = (
        UniqueConstraint("source_course_id", "target_course_id", "pathway_type",
                         name="uq_course_pathway"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_course_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_course_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pathway_type: Mapped[str] = mapped_column(Text, nullable=False, default="articulation")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[str | None] = mapped_column(Text)

    source_course = relationship("Course", foreign_keys=[source_course_id])
    target_course = relationship("Course", foreign_keys=[target_course_id])
