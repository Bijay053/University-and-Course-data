from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DatedCatalogueReviewHistory(Base):
    __tablename__ = "dated_catalogue_review_history"
    __table_args__ = (
        UniqueConstraint(
            "scraped_course_id",
            "revision",
            name="uq_dated_catalogue_review_history_row_revision",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    scraped_course_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("scraped_courses.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


Index(
    "ix_dated_catalogue_review_history_row_revision_desc",
    DatedCatalogueReviewHistory.scraped_course_id,
    DatedCatalogueReviewHistory.revision.desc(),
)