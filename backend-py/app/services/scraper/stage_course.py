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
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ScrapedCourse, ScrapedFieldEvidence
from app.services.auto_publish import should_auto_publish
from app.services.scraper.category import map_course_to_category
from app.services.scraper.completeness import compute_completeness, decide_eligibility
from app.services.scraper.guards import (
    enforce_source_evidence,
    is_blocked_page,
    is_generic_course_category_name,
    should_stage_course,
)

log = logging.getLogger(__name__)


@dataclass
class StageResult:
    saved: bool
    reason: str
    scraped_course_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:  # so existing `if result:` patterns still work
        return self.saved


# ---------------------------------------------------------------------------
# Specialisation name augmentation
# ---------------------------------------------------------------------------
# Some universities (VIT) publish separate pages per specialisation that all
# share the same extracted parent degree name.  We derive the specialisation
# label from the URL path so review-table rows are distinguishable.
#
# Pattern:  /{degree_code}/{degree_code}-{spec-slug}
#   e.g.    /bits/bits-artificial-intelligence-analytics
#           → "Bachelor of IT and Systems (Artificial Intelligence Analytics)"
_SPECIALIZATION_AUGMENT_HOSTS: frozenset[str] = frozenset({
    "vit.edu.au", "www.vit.edu.au",
})

def _augment_specialization_name(course_name: str, source_url: str | None) -> str:
    """Return course_name augmented with specialisation label when the URL encodes one."""
    if not source_url:
        return course_name
    try:
        from urllib.parse import urlparse
        parsed = urlparse(source_url)
        if parsed.netloc not in _SPECIALIZATION_AUGMENT_HOSTS:
            return course_name
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        if len(parts) < 2:
            return course_name
        parent_code = parts[0]        # e.g. "bits"
        spec_slug   = parts[1]        # e.g. "bits-artificial-intelligence-analytics"
        prefix = f"{parent_code}-"
        if spec_slug.startswith(prefix):
            spec_slug = spec_slug[len(prefix):]
        spec_words = spec_slug.replace("-", " ").title()
        if spec_words and spec_words.lower() not in course_name.lower():
            return f"{course_name} ({spec_words})"
    except Exception:  # noqa: BLE001
        pass
    return course_name


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
    values: list[dict[str, Any]] = []
    for ev in evidence[:_MAX_EVIDENCE_ROWS]:
        if not isinstance(ev, dict):
            continue
        field_key = ev.get("field_key")
        if not field_key:
            continue
        # decision_status comes from the pipeline: "selected" for the winning
        # entry, "superseded" for entries overridden by a higher-priority source
        # (e.g. gemini_primary), or "needs_review" by default.
        _ds = (ev.get("decision_status") or "needs_review")[:50]
        values.append(
            {
                "scraped_course_id": scraped_course_id,
                "field_key": str(field_key)[:200],
                "candidate_value": _to_text(ev.get("value")),
                "normalized_value": _to_text(ev.get("normalized") or ev.get("value")),
                "source_url": (ev.get("source_url") or source_url),
                "page_type": ev.get("page_type"),
                "extraction_method": (ev.get("method") or "unknown")[:200],
                "snippet": (ev.get("snippet") or None) and str(ev["snippet"])[:1000],
                "confidence": (
                    (lambda c: None if not math.isfinite(c) else c)(float(ev["confidence"]))
                    if isinstance(ev.get("confidence"), (int, float))
                    else None
                ),
                "decision_status": _ds,
                "selected": _ds == "selected",
            }
        )
    if not values:
        return 0
    # ON CONFLICT DO NOTHING prevents orphaned duplicate evidence rows (e.g.
    # when a previous scrape left evidence behind after its ScrapedCourse was
    # deleted and the sequence later re-issued the same id) from poisoning the
    # entire session with an IntegrityError.  No named constraint is referenced
    # so this works even before the DB index is fully in place.
    stmt = pg_insert(ScrapedFieldEvidence).values(values).on_conflict_do_nothing()
    await db.execute(stmt)
    return len(values)


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

    # Phase A defence-in-depth: refuse to stage a course whose source URL
    # is on the page blocklist (apply / fees / news / faculty / etc.).
    # Discovery should have caught this earlier; if a regression there
    # ever lets one through, this stops the bad row from being saved.
    if source_url:
        blocked, block_reason = is_blocked_page(source_url, payload.get("page_title"))
        if blocked:
            log.info("blocked_page rejected %r: %s (%s)", name, block_reason, source_url)
            return StageResult(False, f"rejected: blocked_page:{block_reason}")

    # Bugs A / B / C (Torrens T007 sweep): staging gate that rejects category
    # landing pages, domestic-only courses, and online-only courses.  Runs
    # AFTER the generic-name guard (cheaper) but BEFORE any DB work (no point
    # hitting the rejection-block query for a page we'll always reject).
    accept, gate_reason = should_stage_course(name, payload, source_url=source_url)
    if not accept:
        log.info("staging_gate rejected %r: %s", name, gate_reason)
        return StageResult(False, f"rejected: {gate_reason}")

    # Within-job URL deduplication: prevent the exact same source URL from
    # being staged twice in one job (can happen if a BFS bug re-queues a URL).
    # We intentionally do NOT dedup by course_name alone — universities like
    # VIT publish separate pages per specialisation (e.g. /bits/bits-ai,
    # /bits/bits-app-dev) that all share the same parent degree name but are
    # genuinely distinct enrolment-level programmes.  Deduping by name
    # silently drops those specialisations from the review queue.
    # For VIT we also augment the course name with the specialisation derived
    # from the URL path so reviewers can distinguish the rows.
    name = _augment_specialization_name(name, source_url)
    try:
        _dup_q = await db.execute(
            select(ScrapedCourse.id)
            .where(
                ScrapedCourse.scrape_job_id == scrape_job_id,
                ScrapedCourse.university_id == university_id,
                ScrapedCourse.course_website == source_url,
            )
            .limit(1)
        )
        _dup = _dup_q.scalar_one_or_none()
        if _dup is not None:
            log.info(
                "stage_course: skipping duplicate URL %r (already staged in job %s)",
                source_url, scrape_job_id,
            )
            return StageResult(False, "rejected: duplicate_url_in_job")
    except Exception as _dep:  # noqa: BLE001 — never abort on dedup check failure
        log.warning("stage_course: within-job dedup check failed for %r: %s", source_url, _dep)

    # Cross-job dedup: delete stale pending/review_ready rows from prior scrape
    # jobs for the same (university_id, course_website).  Without this guard
    # every re-scrape doubles the row count in scraped_courses — new rows from
    # the fresh job land alongside old rows from the previous job, and admins
    # see every course twice in the review queue.
    #
    # Safety: only delete rows whose status is NOT 'approved' or 'published' —
    # those are operator-confirmed and must never be touched here.  The existing
    # preservation block below (lines ~285+) already copies field values from
    # approved rows into the new staging row so no data is lost.
    if source_url:
        try:
            _stale_q = await db.execute(
                select(ScrapedCourse.id)
                .where(
                    ScrapedCourse.university_id == university_id,
                    ScrapedCourse.course_website == source_url,
                    ScrapedCourse.scrape_job_id != scrape_job_id,
                    ScrapedCourse.status.not_in(["approved", "published"]),
                )
            )
            _stale_ids = [row[0] for row in _stale_q.fetchall()]
            if _stale_ids:
                await db.execute(
                    delete(ScrapedCourse).where(ScrapedCourse.id.in_(_stale_ids))
                )
                log.info(
                    "stage_course: cross-job dedup — deleted %d stale row(s) for URL %r (uni %s)",
                    len(_stale_ids),
                    source_url,
                    university_id,
                )
        except Exception as _cdep:  # noqa: BLE001 — never abort on dedup check failure
            log.warning(
                "stage_course: cross-job dedup check failed for %r: %s", source_url, _cdep
            )

    # Phase A: drop critical fields (fee, english tests, location, study_mode,
    # duration) that lack source proof.  Better to publish "unknown" than
    # publish a guess.  The dropped fields are logged so the operator can
    # see WHY a row landed in review with NULLs.
    payload, dropped_fields = enforce_source_evidence(payload, evidence)
    if dropped_fields:
        log.info(
            "source_evidence dropped fields %s for %r (uni %s) — no source_url+snippet proof",
            dropped_fields, name, university_id,
        )

    # ── Confidence gate ───────────────────────────────────────────────────
    # Courses with confidence < 60 (two or more critical fields missing) are
    # not staged.  A missing row is better than a row with misleading data —
    # operators cannot easily spot wrong values but will notice a missing row.
    # Exceptions:
    #   • has_central_fee_page=True already earns the 25 fee points, so
    #     universities like Bond/ECU are not unfairly penalised for JS fees.
    #   • domestic_only courses are blocked earlier; this gate never fires on them.
    from app.services.scraper.confidence import (  # noqa: PLC0415 — local to avoid circular import
        CONFIDENCE_WARN as _CONF_GATE,
        score_payload as _score_payload,
    )
    _cg = _score_payload(payload)
    if _cg["score"] < _CONF_GATE:
        log.info(
            "stage_course: confidence %d/100 < %d — skipping %r (missing: %s)",
            _cg["score"], _CONF_GATE, name, ", ".join(_cg.get("missing", [])),
        )
        return StageResult(
            False,
            f"rejected: confidence_{_cg['score']}_missing_{'_'.join(_cg.get('missing', []))}",
        )

    # ── Preserve existing valid data ──────────────────────────────────────
    # When a re-scrape cannot extract a field that was successfully captured
    # in a previous approved/published row, keep the old value rather than
    # writing NULL.  This prevents a temporary website change or extractor
    # regression from degrading already-reviewed data.
    _PRESERVE_FIELDS = (
        "international_fee", "domestic_fee", "fee_term",
        "ielts_overall", "pte_overall", "toefl_overall",
        "cambridge_overall", "duolingo_overall",
        "ielts_listening", "ielts_reading", "ielts_writing", "ielts_speaking",
        "duration", "duration_term",
        "intake_months",
        "study_mode", "course_location",
    )
    try:
        _exist_q = await db.execute(
            select(ScrapedCourse)
            .where(
                ScrapedCourse.university_id == university_id,
                ScrapedCourse.course_name == name,
                ScrapedCourse.status.in_(["approved", "published"]),
            )
            .order_by(ScrapedCourse.created_at.desc())
            .limit(1)
        )
        _exist = _exist_q.scalar_one_or_none()
        if _exist:
            preserved: list[str] = []
            for _fld in _PRESERVE_FIELDS:
                if payload.get(_fld) is None and getattr(_exist, _fld, None) is not None:
                    payload[_fld] = getattr(_exist, _fld)
                    preserved.append(_fld)
            if preserved:
                log.info(
                    "stage_course: preserved %d field(s) from existing approved row for %r: %s",
                    len(preserved), name, preserved,
                )
    except Exception as _pex:  # noqa: BLE001 — never abort staging on preservation failure
        log.warning("stage_course: existing-data preservation query failed for %r: %s", name, _pex)

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

    # All rejection reasons are transient: every re-scrape re-evaluates every
    # course from scratch.  If the extraction code changes or a university
    # updates its page, a previously rejected course gets a fresh chance
    # automatically without any DB cleanup.
    # (No blocking check — fall through to full extraction + guard evaluation.)

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

    # Sanitize NaN/Inf floats before writing — PostgreSQL accepts NaN as a
    # FLOAT value but Python's JSON encoder will later raise ValueError on it.
    def _clean(v: Any) -> Any:
        return None if isinstance(v, float) and not math.isfinite(v) else v

    sc = ScrapedCourse(
        scrape_job_id=scrape_job_id,
        university_id=university_id,
        course_name=name,
        **{k: _clean(v) for k, v in payload.items() if hasattr(ScrapedCourse, k) and k != "course_name"},
    )
    db.add(sc)
    try:
        await db.flush()  # need sc.id for the FK on evidence rows
    except Exception as exc:  # noqa: BLE001
        # Explicit rollback prevents returning a poisoned connection to the
        # pool. Without this, asyncpg leaves the connection in a
        # "transaction aborted" state; every subsequent course on the same
        # pooled connection then fails with InFailedSQLTransactionError
        # even though the root error was something unrelated (e.g. a
        # missing column from an unapplied migration).
        await db.rollback()
        log.warning(
            "stage_course: initial flush failed for %r (uni %s): %s — rolling back",
            name, university_id, exc,
        )
        return StageResult(False, f"flush failed: {exc}")

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
