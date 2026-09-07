from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, Numeric, Text, event, func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

REVIEW_URL_IDENTITY_CONSTRAINT = "uq_scraped_courses_job_review_url_identity"


def integrity_constraint_name(exc: IntegrityError) -> str | None:
    """Read a PostgreSQL constraint name through SQLAlchemy/asyncpg wrappers."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        direct = getattr(current, "constraint_name", None)
        if direct:
            return str(direct)
        diag = getattr(current, "diag", None)
        diagnosed = getattr(diag, "constraint_name", None)
        if diagnosed:
            return str(diagnosed)
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None)
    return None


class ScrapedCourse(Base):
    __tablename__ = "scraped_courses"
    __table_args__ = (
        Index(
            REVIEW_URL_IDENTITY_CONSTRAINT,
            "university_id",
            "canonical_course_url",
            "scrape_job_id",
            unique=True,
            postgresql_where=text("status NOT IN ('approved', 'published')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scrape_job_id: Mapped[str] = mapped_column(Text, nullable=False)
    university_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("universities.id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("courses.id", ondelete="SET NULL")
    )
    course_name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(Text)
    sub_category: Mapped[str | None] = mapped_column(Text)
    course_website: Mapped[str | None] = mapped_column(Text)
    canonical_course_url: Mapped[str | None] = mapped_column(Text)
    duration: Mapped[float | None] = mapped_column(Numeric(6, 2))
    duration_term: Mapped[str | None] = mapped_column(Text)
    study_mode: Mapped[str | None] = mapped_column(Text)
    degree_level: Mapped[str | None] = mapped_column(Text)
    study_load: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    other_requirement: Mapped[str | None] = mapped_column(Text)
    international_fee: Mapped[float | None] = mapped_column(Float)
    fee_term: Mapped[str | None] = mapped_column(Text)
    fee_year: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(Text)
    ielts_overall: Mapped[float | None] = mapped_column(Float)
    ielts_listening: Mapped[float | None] = mapped_column(Float)
    ielts_speaking: Mapped[float | None] = mapped_column(Float)
    ielts_writing: Mapped[float | None] = mapped_column(Float)
    ielts_reading: Mapped[float | None] = mapped_column(Float)
    pte_overall: Mapped[float | None] = mapped_column(Float)
    pte_listening: Mapped[float | None] = mapped_column(Float)
    pte_speaking: Mapped[float | None] = mapped_column(Float)
    pte_writing: Mapped[float | None] = mapped_column(Float)
    pte_reading: Mapped[float | None] = mapped_column(Float)
    toefl_overall: Mapped[float | None] = mapped_column(Float)
    toefl_listening: Mapped[float | None] = mapped_column(Float)
    toefl_speaking: Mapped[float | None] = mapped_column(Float)
    toefl_writing: Mapped[float | None] = mapped_column(Float)
    toefl_reading: Mapped[float | None] = mapped_column(Float)
    cambridge_overall: Mapped[float | None] = mapped_column(Float)
    duolingo_overall: Mapped[float | None] = mapped_column(Float)
    duolingo_accepted: Mapped[bool | None] = mapped_column(Boolean)
    cambridge_accepted: Mapped[bool | None] = mapped_column(Boolean)
    pte_accepted: Mapped[bool | None] = mapped_column(Boolean)
    toefl_accepted: Mapped[bool | None] = mapped_column(Boolean)
    intake_months: Mapped[list | None] = mapped_column(JSONB)
    intake_days: Mapped[int | None] = mapped_column(Integer)
    academic_level: Mapped[str | None] = mapped_column(Text)
    academic_score: Mapped[float | None] = mapped_column(Float)
    score_type: Mapped[str | None] = mapped_column(Text)
    academic_country: Mapped[str | None] = mapped_column(Text)
    scholarship: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    completeness: Mapped[int | None] = mapped_column(Integer)
    course_location: Mapped[str | None] = mapped_column(Text)
    student_market: Mapped[str | None] = mapped_column(Text)
    delivery_mode: Mapped[str | None] = mapped_column(Text)
    international_eligible: Mapped[bool | None] = mapped_column(Boolean)
    on_campus_available: Mapped[bool | None] = mapped_column(Boolean)
    eligibility_status: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    eligibility_reason: Mapped[str | None] = mapped_column(Text)
    eligibility_confidence: Mapped[float | None] = mapped_column(Float)
    auto_publish_status: Mapped[str] = mapped_column(Text, nullable=False, default="pending_review")
    decision_score: Mapped[float | None] = mapped_column(Float)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_method: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    scrape_warnings: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    has_central_fee_page: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cricos_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    avg_verification_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    pub_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    pub_score_breakdown: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    pub_decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    pub_decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


@event.listens_for(ScrapedCourse, "before_insert")
@event.listens_for(ScrapedCourse, "before_update")
def _sync_canonical_course_url(
    _mapper: object,
    _connection: object,
    target: ScrapedCourse,
) -> None:
    """Keep indexed URL identity synchronized for every ORM write path."""
    from app.services.scraper.url_identity import canonical_course_url_key

    target.canonical_course_url = (
        canonical_course_url_key(target.course_website) or None
    )
