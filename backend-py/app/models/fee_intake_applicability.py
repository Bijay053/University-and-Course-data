"""Intake applicability attached to a published tuition row, never a scalar guess."""
from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class FeeIntakeApplicability(Base):
    __tablename__ = "fee_intake_applicability"
    __table_args__ = (
        CheckConstraint("entry_year BETWEEN 2000 AND 2100", name="ck_fee_intake_year"),
        CheckConstraint("jsonb_typeof(months) = 'array' AND jsonb_array_length(months) > 0",
                        name="ck_fee_intake_months"),
    )
    fee_id: Mapped[int] = mapped_column(
        ForeignKey("fees.id", ondelete="CASCADE"), primary_key=True)
    entry_year: Mapped[int] = mapped_column(Integer, nullable=False)
    months: Mapped[list] = mapped_column(JSONB, nullable=False)
    source_urls: Mapped[list] = mapped_column(JSONB, nullable=False)
    source_digest: Mapped[str] = mapped_column(Text, nullable=False)
