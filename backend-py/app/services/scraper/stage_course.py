"""Stage a discovered course as a ``scraped_courses`` row.

Bug #2 fix: this returns a ``StageResult`` dataclass with explicit
``saved`` + ``reason`` so the caller can log what happened. The Node API
returned bare ``True`` on success and bare ``False`` on every failure, which
made debugging staging issues impossible.

Bug #7 fix: the rejection-block window is read from ``settings.rejection_block_days``
(default 7), not 30 like the Node hardcode.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ScrapedCourse


@dataclass
class StageResult:
    saved: bool
    reason: str
    scraped_course_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:  # so existing `if result:` patterns still work
        return self.saved


async def stage_course(
    db: AsyncSession,
    *,
    scrape_job_id: str,
    university_id: int,
    course_name: str,
    payload: dict[str, Any],
) -> StageResult:
    name = (course_name or "").strip()
    if len(name) < 3:
        return StageResult(False, "course_name too short")

    # Bug #7: skip if a recent rejection exists (window = settings.rejection_block_days).
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.rejection_block_days)
    recent_rejection = (
        await db.execute(
            select(ScrapedCourse.id)
            .where(
                ScrapedCourse.university_id == university_id,
                func.lower(ScrapedCourse.course_name) == name.lower(),
                ScrapedCourse.status == "rejected",
                ScrapedCourse.created_at >= cutoff,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if recent_rejection:
        return StageResult(
            False,
            f"recently rejected (within {settings.rejection_block_days}d)",
            extra={"rejected_id": recent_rejection},
        )

    sc = ScrapedCourse(
        scrape_job_id=scrape_job_id,
        university_id=university_id,
        course_name=name,
        **{k: v for k, v in payload.items() if hasattr(ScrapedCourse, k)},
    )
    db.add(sc)
    await db.flush()
    await db.commit()
    return StageResult(True, "staged", scraped_course_id=sc.id)
