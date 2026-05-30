"""Phase 10 — change_detector.py

Compares the latest scrape snapshot to the previous one for the same
university and emits ``CourseChangeEvent`` rows.

Safety rules (spec §8):
  - Normalise values with Phase 9 normalizers before comparing so that
    formatting-only differences (e.g. "$45,000" vs "45000") are ignored.
  - Skip field changes where both snapshots have confidence < 70 (unreliable).
  - Critical-field changes are gated more strictly (confidence ≥ 70 required
    on the *new* snapshot).
  - Keep full history — never overwrite old event rows.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.course_snapshot import CourseSnapshot
from app.models.course_change_event import CourseChangeEvent

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Severity classification
# ---------------------------------------------------------------------------

_CRITICAL_FIELDS: frozenset[str] = frozenset({
    "international_fee", "intake_months", "ielts_overall", "other_requirement",
})
_MAJOR_FIELDS: frozenset[str] = frozenset({
    "duration", "course_location",
})
_MINOR_FIELDS: frozenset[str] = frozenset({
    "study_mode", "degree_level", "academic_level",
    "academic_score", "pte_overall", "toefl_overall",
})
# Anything else → "info"

_COMPARE_FIELDS: tuple[str, ...] = (
    "international_fee",
    "intake_months",
    "ielts_overall",
    "other_requirement",
    "duration",
    "course_location",
    "study_mode",
    "degree_level",
    "academic_score",
    "academic_level",
    "pte_overall",
    "toefl_overall",
    "course_url",
)

_MIN_CONFIDENCE = 70.0


def _severity(field_name: str, change_type: str) -> str:
    if change_type in ("new_course", "removed_course"):
        return "major"
    if field_name in _CRITICAL_FIELDS:
        return "critical"
    if field_name in _MAJOR_FIELDS:
        return "major"
    if field_name in _MINOR_FIELDS:
        return "minor"
    return "info"


# ---------------------------------------------------------------------------
# Value normalisation helpers
# ---------------------------------------------------------------------------

def _norm(field: str, value: Any) -> str | None:
    """Return a normalised string for *value* suitable for equality comparison.

    Uses Phase 9 normalizers for fields that have them; falls back to a
    simple lower-strip for others.  Returns None to signal "empty".
    """
    if value is None:
        return None
    if isinstance(value, list):
        joined = ",".join(sorted(str(v).lower().strip() for v in value if v is not None))
        return joined or None

    try:
        from app.services.scraper.verification_engine import _normalize_value
        result = _normalize_value(field, str(value))
        return result
    except Exception:  # noqa: BLE001
        return " ".join(str(value).lower().split()) or None


def _course_key(name: str) -> str:
    """Normalised course name used for cross-snapshot matching."""
    return " ".join(name.lower().split())


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def detect_changes(
    university_id: int,
    scrape_job_id: str,
    db: AsyncSession,
) -> int:
    """Diff the current job's snapshots against the previous job's snapshots.

    Returns the number of ``CourseChangeEvent`` rows written (0 if this is the
    first scrape for this university or no changes were found).
    """
    # 1. Load current snapshots
    curr_q = await db.execute(
        select(CourseSnapshot).where(
            CourseSnapshot.university_id == university_id,
            CourseSnapshot.scrape_job_id == scrape_job_id,
        )
    )
    current_snaps = curr_q.scalars().all()
    if not current_snaps:
        log.info("[CHANGE_DETECT] no snapshots for run=%s — skip", scrape_job_id)
        return 0

    # 2. Find the most-recent *previous* job that has snapshots for this uni
    prev_job_q = await db.execute(
        select(
            CourseSnapshot.scrape_job_id,
            sa_func.max(CourseSnapshot.snapshotted_at).label("last_at"),
        )
        .where(
            CourseSnapshot.university_id == university_id,
            CourseSnapshot.scrape_job_id != scrape_job_id,
        )
        .group_by(CourseSnapshot.scrape_job_id)
        .order_by(sa_func.max(CourseSnapshot.snapshotted_at).desc())
        .limit(1)
    )
    prev_row = prev_job_q.first()
    if prev_row is None:
        log.info(
            "[CHANGE_DETECT] uni=%s run=%s: no prior snapshot — baseline established",
            university_id, scrape_job_id,
        )
        return 0

    prev_job_id = prev_row[0]
    prev_snaps_q = await db.execute(
        select(CourseSnapshot).where(
            CourseSnapshot.university_id == university_id,
            CourseSnapshot.scrape_job_id == prev_job_id,
        )
    )
    prev_snaps = prev_snaps_q.scalars().all()

    curr_by_name: dict[str, CourseSnapshot] = {
        _course_key(s.course_name): s for s in current_snaps
    }
    prev_by_name: dict[str, CourseSnapshot] = {
        _course_key(s.course_name): s for s in prev_snaps
    }

    events: list[CourseChangeEvent] = []

    # 3. New courses (present in current, absent in previous)
    for key, snap in curr_by_name.items():
        if key not in prev_by_name:
            events.append(CourseChangeEvent(
                university_id=university_id,
                course_id=snap.course_id,
                course_name=snap.course_name,
                scrape_job_id=scrape_job_id,
                field_name="course_name",
                old_value=None,
                new_value=snap.course_name,
                change_type="new_course",
                severity="major",
                confidence_before=None,
                confidence_after=snap.avg_verification_confidence,
                status="new",
            ))

    # 4. Removed courses (present in previous, absent in current)
    for key, snap in prev_by_name.items():
        if key not in curr_by_name:
            events.append(CourseChangeEvent(
                university_id=university_id,
                course_id=snap.course_id,
                course_name=snap.course_name,
                scrape_job_id=scrape_job_id,
                field_name="course_name",
                old_value=snap.course_name,
                new_value=None,
                change_type="removed_course",
                severity="major",
                confidence_before=snap.avg_verification_confidence,
                confidence_after=None,
                status="new",
            ))

    # 5. Field-level changes for matched courses
    for key in curr_by_name:
        if key not in prev_by_name:
            continue

        curr = curr_by_name[key]
        prev = prev_by_name[key]

        curr_conf: float = curr.avg_verification_confidence or 0.0
        prev_conf: float = prev.avg_verification_confidence or 0.0

        # Skip entirely if neither snapshot was confident enough
        if curr_conf < _MIN_CONFIDENCE and prev_conf < _MIN_CONFIDENCE:
            continue

        # Quick-exit: page_hash matches → all compared fields are identical
        if curr.page_hash and prev.page_hash and curr.page_hash == prev.page_hash:
            continue

        for field in _COMPARE_FIELDS:
            old_raw = getattr(prev, field, None)
            new_raw = getattr(curr, field, None)

            old_norm = _norm(field, old_raw)
            new_norm = _norm(field, new_raw)

            # Formatting-only change (same after normalisation) → skip
            if old_norm == new_norm:
                continue
            # Both empty → skip
            if old_norm is None and new_norm is None:
                continue

            # Critical-field gate: require high confidence on the current snap
            if field in _CRITICAL_FIELDS and curr_conf < _MIN_CONFIDENCE:
                continue

            events.append(CourseChangeEvent(
                university_id=university_id,
                course_id=curr.course_id,
                course_name=curr.course_name,
                scrape_job_id=scrape_job_id,
                field_name=field,
                old_value=str(old_raw) if old_raw is not None else None,
                new_value=str(new_raw) if new_raw is not None else None,
                change_type="field_change",
                severity=_severity(field, "field_change"),
                confidence_before=prev_conf if prev_conf > 0 else None,
                confidence_after=curr_conf if curr_conf > 0 else None,
                status="new",
            ))

    if events:
        db.add_all(events)
        await db.commit()
        n_new = sum(1 for e in events if e.change_type == "new_course")
        n_rem = sum(1 for e in events if e.change_type == "removed_course")
        n_fld = sum(1 for e in events if e.change_type == "field_change")
        n_crit = sum(1 for e in events if e.severity == "critical")
        log.info(
            "[CHANGE_DETECT] uni=%s run=%s: %d events "
            "(new=%d removed=%d field=%d critical=%d)",
            university_id, scrape_job_id, len(events),
            n_new, n_rem, n_fld, n_crit,
        )
    else:
        log.info(
            "[CHANGE_DETECT] uni=%s run=%s: no changes detected (prev=%s)",
            university_id, scrape_job_id, prev_job_id,
        )

    return len(events)
