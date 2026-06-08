"""SQLAlchemy model for page_snapshots table.

Records metadata about every HTML/JSON/PDF snapshot saved to S3 during a
scrape job.  The actual content lives in S3; this table is a lightweight
index for lookup, replay, and audit.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PageSnapshot(Base):
    __tablename__ = "page_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    university_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("universities.id", ondelete="CASCADE"),
        nullable=False,
    )
    scrape_job_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("scrape_runtime_jobs.runtime_job_id", ondelete="CASCADE"),
        nullable=False,
    )

    # The URL whose content was captured
    course_url: Mapped[str] = mapped_column(Text, nullable=False)
    # SHA-256 hex digest of course_url (first 16 chars) — used as S3 path segment
    url_hash: Mapped[str] = mapped_column(Text, nullable=False)

    # html | json | pdf | repair | failed
    snapshot_type: Mapped[str] = mapped_column(Text, nullable=False, default="html")

    # Full S3 object key — e.g. universities/42/job_abc/a1b2c3d4/rendered.html
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # HTTP response metadata
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_length: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # How the page was fetched: httpx | curl_cffi | scrape_do | browser | wayback | api
    fetch_method: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Extractor version string (git short SHA or semver) if available
    extractor_version: Mapped[str | None] = mapped_column(Text, nullable=True)

    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # Indexes for common access patterns
    __table_args__ = (
        Index("ix_page_snapshots_job", "scrape_job_id"),
        Index("ix_page_snapshots_uni_url", "university_id", "url_hash"),
        Index("ix_page_snapshots_url_hash", "url_hash"),
    )
