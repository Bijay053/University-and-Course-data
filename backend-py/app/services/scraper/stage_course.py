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
from app.services.scraper.category import map_course_to_category
from app.services.scraper.completeness import compute_completeness, decide_eligibility
from app.services.scraper.guards import is_generic_course_category_name, should_stage_course

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

    # Diff item G (MIGRATION_AUDIT.md §6): reject staging when course_name
    # is just a catalogue header ("Business", "Master's Degrees", "Single
    # Subjects"). These slip through when discovery walks a category
    # landing page and treats every nav item as a real course; keeping
    # them out of scraped_courses is cheaper than rejecting them later
    # in the review modal.
    if is_generic_course_category_name(name):
        return StageResult(False, "rejected: generic category page")

    # Bugs A / B / C (Torrens T007 sweep): staging gate that rejects category
    # landing pages, domestic-only courses, and online-only courses.  Runs
    # AFTER the generic-name guard (cheaper) but BEFORE any DB work (no point
    # hitting the rejection-block query for a page we'll always reject).
    accept, gate_reason = should_stage_course(name, payload, source_url=source_url)
    if not accept:
        log.info("staging_gate rejected %r: %s", name, gate_reason)
        return StageResult(False, f"rejected: {gate_reason}")

    # Diff item R (MIGRATION_AUDIT.md §6): category safety net. The
    # single_course pipeline runs map_course_to_category before staging,
    # but courses that arrive via other code paths (or future paths) can
    # still land with an empty category. Re-run the keyword pre-map here
    # so every staged row has the best category we can compute from the
    # course name alone — the body-text classifier and AI fallbacks
    # already ran upstream and don't re-run here.
    if not payload.get("category"):
        try:
            det = map_course_to_category(name)
        except Exception as exc:  # noqa: BLE001 — never let categorisation abort staging
            log.warning("category safety-net failed for %s: %s", name, exc)
            det = None
        if det:
            payload["category"] = det.get("category")
            if not payload.get("sub_category"):
                payload["sub_category"] = det.get("sub_category")

    # Bug #7: skip if a recent rejection exists (window = settings.rejection_block_days).
    #
    # Rejection reason awareness: only block on "permanent" disqualifiers
    # (category_landing_page, manual_reject, or unknown/NULL).
    # Transient reasons — extractor_bug, bulk_reset, no_international_fee,
    # expired — do NOT trigger the cooldown so a code-side fix can re-stage
    # the course on the very next run without DB surgery.
    # online_only is transient: if the institution adds campus options the
    # course should be re-staged automatically on the next scrape without
    # manual DB cleanup.
    _TRANSIENT_REJECTION_REASONS = frozenset({
        "extractor_bug",
        "bulk_reset",
        "no_international_fee",
        "expired",
        "online_only",
    })
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.rejection_block_days)
    _recent_row = (
        await db.execute(
            select(ScrapedCourse.id, ScrapedCourse.rejection_reason, ScrapedCourse.created_at)
            .where(
                ScrapedCourse.university_id == university_id,
                func.lower(ScrapedCourse.course_name) == name.lower(),
                ScrapedCourse.status == "rejected",
                ScrapedCourse.created_at >= cutoff,
            )
            .order_by(ScrapedCourse.created_at.desc())
            .limit(1)
        )
    ).first()
    if _recent_row:
        _rej_id, _rej_reason, _rej_at = _recent_row
        if _rej_reason not in _TRANSIENT_REJECTION_REASONS:
            _rej_date = _rej_at.strftime("%Y-%m-%d") if _rej_at else "unknown"
            return StageResult(
                False,
                f"recently rejected (within {settings.rejection_block_days}d, rejected {_rej_date})",
                extra={"rejected_id": _rej_id, "rejection_reason": _rej_reason},
            )

    # Canonicalize degree_level to the standard apostrophe-s forms used by
    # the degree_level extractor and the sibling-cache bucket logic.
    # Older scrapes / AI fallbacks sometimes returned bare "Master" or
    # "Bachelor" (without the "'s") producing duplicate variants in the DB
    # that break every level-based query and filter.
    _DEGREE_LEVEL_CANONICAL: dict[str, str] = {
        "bachelor":  "Bachelor's",
        "master":    "Master's",
        "doctorate": "Doctorate",
        "doctor":    "Doctorate",
    }
    _raw_dl = (payload.get("degree_level") or "").strip()
    _canon = _DEGREE_LEVEL_CANONICAL.get(_raw_dl.lower())
    if _canon:
        payload = dict(payload)
        payload["degree_level"] = _canon

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

    # Diff item I (MIGRATION_AUDIT.md §6): cross-evidence conflict
    # detection. Runs after evidence rows are flushed (so they have IDs
    # the FieldConflict.evidence_a_id/b_id FKs can reference) but before
    # commit, so the conflict rows land in the same transaction. Wrapped
    # in try/except — a detector failure must never block the staging
    # itself, the modal can render without conflicts.
    #
    # IMPORTANT: AsyncSessionLocal is configured with autoflush=False, so
    # `_persist_evidence` only db.add()'d the rows — they don't exist
    # at the database level until we explicitly flush. Without this
    # flush the detector's SELECT returns zero rows and we'd silently
    # produce no conflicts. (Caught by code review on PR-1.)
    conflicts_written = 0
    if evidence_count:
        try:
            await db.flush()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "evidence flush before conflict detection failed for sc %s: %s",
                sc.id, exc,
            )
        try:
            from app.services.review.conflicts import detect_and_persist_conflicts

            conflicts_written = await detect_and_persist_conflicts(db, sc.id)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "conflict detection failed for sc %s (uni %s): %s",
                sc.id, university_id, exc,
            )

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
        extra={
            "evidence_rows": evidence_count,
            "completeness": sc.completeness,
            "conflicts": conflicts_written,
        },
    )
