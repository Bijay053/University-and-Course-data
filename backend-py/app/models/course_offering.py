"""Verified campus tuition, owned by one published award identity."""
from sqlalchemy import Float, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CourseOffering(Base):
    __tablename__ = "course_offerings"
    __table_args__ = (UniqueConstraint("course_id", "location_key", name="uq_course_offering_location"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    location_key: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str] = mapped_column(Text, nullable=False)
    fee_amount: Mapped[float | None] = mapped_column(Float)
    fee_currency: Mapped[str | None] = mapped_column(Text)
    fee_term: Mapped[str | None] = mapped_column(Text)
    fee_year: Mapped[int | None] = mapped_column(Integer)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
