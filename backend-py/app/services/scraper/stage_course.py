"""Stage a discovered course as a ``scraped_courses`` row.

Bug #2 fix: this returns a ``StageResult`` dataclass with explicit
``saved`` + ``reason`` so the caller can log what happened. The Node API
returned bare ``True`` on success and bare ``False`` on every failure, which
made debugging staging issues impossible.

Bug #7 fix: the rejection-block window is read from ``settings.rejection_block_days``
(default 7), not 30 like the Node hardcode.

Bug C/D fix: this is also where we (a) compute completeness + auto-publish
+ eligibility status so the Review table's Score / Level / Mode / Category
columns and the "Publish blocked" reasoning are populated, and (b) persist
the per-field evidence rows so the Evidence Review modal renders content
instead of a blank body.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ScrapedCourse, ScrapedFieldEvidence
from app.services.auto_publish import should_auto_publish
from app.services.scraper.completeness import compute_completeness, decide_eligibility

log = logging.getLogger(__name__)


@dataclass
class StageResult:
    saved: bool
    reason: str
    scraped_course_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:  # so existing `if result:` patterns still work
        return self.saved


# Cap evidence rows per course. A pathological page can spam dozens of
# duplicate matches for the same field; keeping the table lean keeps the
# review modal fast and bounded.
_MAX_EVIDENCE_ROWS = 200


def _to_text(val: Any) -> str | None:
    """Best-effort serialization for storing ``candidate_value`` as TEXT."""
    if val is None:
        return None
    if isinstance(val, (str, int, float, bool)):
        return str(val)
    try:
        import json

        return json.dumps(val, default=str)[:1000]
    except Exception:  # noqa: BLE001
        return str(val)[:1000]


async def _persist_evidence(
    db: AsyncSession,
    *,
    scraped_course_id: int,
    evidence: list[dict[str, Any]],
    source_url: str | None,
) -> int:
    if not evidence:
        return 0
    rows: list[ScrapedFieldEvidence] = []
    for ev in evidence[:_MAX_EVIDENCE_ROWS]:
        if not isinstance(ev, dict):
            continue
        field_key = ev.get("field_key")
        if not field_key:
            continue
        rows.append(
            ScrapedFieldEvidence(
                scraped_course_id=scraped_course_id,
                field_key=str(field_key)[:200],
                candidate_value=_to_text(ev.get("value")),
                normalized_value=_to_text(ev.get("normalized") or ev.get("value")),
                source_url=(ev.get("source_url") or source_url),
                page_type=ev.get("page_type"),
                extraction_method=(ev.get("method") or "unknown")[:200],
                snippet=(ev.get("snippet") or None) and str(ev["snippet"])[:1000],
                confidence=(
                    float(ev["confidence"])
                    if isinstance(ev.get("confidence"), (int, float))
                    else None
                ),
                # Defaults are fine for validation_status / decision_status /
                # selected — operator review fills these in via the modal.
            )
        )
    if not rows:
        return 0
    db.add_all(rows)
    return len(rows)


async def stage_course(
    db: AsyncSession,
    *,
    scrape_job_id: str,
    university_id: int,
    course_name: str,
    payload: dict[str, Any],
    evidence: list[dict[str, Any]] | None = None,
    source_url: str | None = None,
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
        **{k: v for k, v in payload.items() if hasattr(ScrapedCourse, k) and k != "course_name"},
    )
    db.add(sc)
    await db.flush()  # need sc.id for the FK on evidence rows

    # ----- Bug C: completeness + eligibility + auto_publish -----
    # Computed against the in-memory ScrapedCourse before commit so the row
    # lands fully populated in one transaction. Defensive try/except — a
    # scoring failure must never lose the staged row itself.
    try:
        comp = compute_completeness(sc)
        sc.completeness = comp.score
        decision = decide_eligibility(sc, comp)
        sc.eligibility_status = decision.status
        sc.eligibility_reason = decision.reason or None
        ap = should_auto_publish(sc)
        # Map auto_publish boolean to the three labels the UI renders.
        sc.auto_publish_status = "ready" if ap.auto_publish else "review"
        sc.decision_score = ap.score
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "completeness/auto_publish scoring failed for %s (uni %s): %s",
            name, university_id, exc,
        )
        # Leave defaults; row still gets saved.

    # ----- Bug D: persist field evidence -----
    # Atomicity contract: evidence rows must commit alongside the parent
    # ScrapedCourse, or neither commits. A partial write (parent row staged,
    # evidence missing) is exactly the Bug D failure mode we're fixing —
    # the review modal would render blank and the operator wouldn't know
    # why. So on persistence failure we roll back the whole transaction
    # and return a failed StageResult.
    evidence_count = 0
    try:
        evidence_count = await _persist_evidence(
            db,
            scraped_course_id=sc.id,
            evidence=evidence or [],
            source_url=source_url or payload.get("course_website"),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "evidence persistence failed for sc %s (uni %s): %s — rolling back",
            sc.id, university_id, exc,
        )
        await db.rollback()
        return StageResult(False, f"evidence persistence failed: {exc}")

    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        log.warning("stage_course commit failed for uni %s: %s", university_id, exc)
        return StageResult(False, f"commit failed: {exc}")

    return StageResult(
        True,
        "staged",
        scraped_course_id=sc.id,
        extra={"evidence_rows": evidence_count, "completeness": sc.completeness},
    )
