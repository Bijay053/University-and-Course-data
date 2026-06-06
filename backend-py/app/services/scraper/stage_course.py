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
import re
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
    """Return course_name augmented with specialisation label when the URL encodes one.

    VIT URL conventions:
      /<program>/<program>-<spec>   e.g. /bits/bits-artificial-intelligence-analytics
      /<parent>/<abbrev>-<spec>     e.g. /bachelor-of-business/bbus-accounting
      /<program>/<code>             e.g. /mits/gdits  (standalone degree, NOT a spec)
      /vocational/<aqf>-<name>      e.g. /vocational/ict40120-certificate-iv-in-...

    Algorithm: split spec_slug on first hyphen.
      - No hyphen → standalone degree URL (gdits, gcba) → do not augment.
      - First component contains a digit → AQF unit code (ict40120) → do not augment.
      - Otherwise → first component is the program short code; strip it and use the
        rest as the specialisation label.  This covers both the "bits-*" pattern
        (where parent matches short code) and the "bbus-*" pattern (where parent is
        long-form but the slug uses the abbreviation).
    """
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
        spec_slug = parts[1]  # e.g. "bits-artificial-intelligence-analytics"

        # Split on the first hyphen only.
        slug_parts = spec_slug.split("-", 1)
        if len(slug_parts) < 2:
            # Single-component slug (e.g. "gdits", "gcba") — standalone degree URL,
            # not a specialisation of the parent.
            return course_name

        first_part, rest = slug_parts
        if not first_part.isalpha():
            # First component contains digits → AQF national unit code (e.g.
            # "ict40120", "sit40521") embedded in a vocational course URL.  The
            # full slug does not represent a parent+spec hierarchy.
            return course_name

        # `rest` is the spec label slug (e.g. "artificial-intelligence-analytics",
        # "accounting", "finance").  Use _smart_case so prepositions ("and",
        # "of", "in", …) stay lowercase inside the spec label.
        try:
            from app.services.scraper.extractors.course_name import _smart_case
            spec_words = _smart_case(rest.replace("-", " "))
        except Exception:  # noqa: BLE001
            spec_words = rest.replace("-", " ").title()
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

    # Normalize source_url before any dedup or storage.
    # 1. Strip URL fragment (#section-id) — the same page content is served
    #    regardless of the fragment; differing anchors from sitemap vs BFS
    #    would otherwise produce duplicate staged rows.
    # 2. Strip trailing slash so /course/foo and /course/foo/ are treated as
    #    one URL.  Only strip when the URL has a path beyond the domain root
    #    so we don't mangle bare origins like "https://www.flinders.edu.au/".
    if source_url:
        _frag_pos = source_url.find("#")
        if _frag_pos != -1:
            source_url = source_url[:_frag_pos]
        if source_url.endswith("/") and source_url.count("/") > 3:
            source_url = source_url.rstrip("/")

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

    # ── Confidence log (informational only — does NOT gate staging) ────────
    # Courses with a low confidence score get a scrape_warning so operators
    # can see at a glance why a row landed in review.  We no longer hard-reject
    # here: the staging gate is (a) degree-qualified name + (b) international_fee
    # via should_stage_course().  Everything that passes that gate is staged;
    # the eligibility / auto_publish_status system then decides review vs ready.
    from app.services.scraper.confidence import (  # noqa: PLC0415
        CONFIDENCE_WARN as _CONF_GATE,
        score_payload as _score_payload,
    )
    _cg = _score_payload(payload)
    if _cg["score"] < _CONF_GATE:
        log.info(
            "stage_course: confidence %d/100 — staging %r with missing fields: %s",
            _cg["score"], name, ", ".join(_cg.get("missing", [])),
        )
        payload.setdefault("scrape_warnings", [])
        if "confidence_low" not in payload["scrape_warnings"]:
            payload["scrape_warnings"].append("confidence_low")

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
                    _val = getattr(_exist, _fld)
                    payload[_fld] = _val
                    preserved.append(_fld)
                    # Emit a traceable evidence entry so the review panel shows
                    # where this value came from — not silently invisible.
                    evidence.append(
                        {
                            "field_key": _fld,
                            "value": _val,
                            "confidence": 0.5,
                            "method": "approved_row:inherited",
                            "snippet": (
                                f"Carried forward from previously approved row "
                                f"(id={_exist.id}, status={_exist.status}): {_fld}={_val}"
                            ),
                            "needs_review": True,
                        }
                    )
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

    # ── DB taxonomy canonicalisation ─────────────────────────────────────────
    # Last line of defence: if a sub_category survived to this point with a
    # value that came from Gemini (e.g. "Applied Cyber Security"), match it
    # against the live `course_sub_categories` rows for the same category and
    # snap it to the existing canonical name ("Cyber Security") whenever
    # there is a strong fuzzy match. This prevents Gemini's free-text guesses
    # from fragmenting the taxonomy with near-duplicate auto-added rows.
    #
    # The matcher is conservative — when no existing row passes the 50%
    # token-overlap threshold, the raw value flows through unchanged so a
    # genuinely new discipline is still surfaced for the reviewer.
    _cat_for_match = payload.get("category")
    _sub_for_match = payload.get("sub_category")
    if _cat_for_match and _sub_for_match:
        try:
            from app.services.sub_category_matcher import resolve_sub_category
            # Wrap in a nested transaction (SAVEPOINT) so that any DB error
            # (e.g. "relation course_sub_categories does not exist" on a
            # fresh clone that hasn't run migration 040 yet) rolls back only
            # this lookup — not the outer staging INSERT.  Without the
            # savepoint, a ProgrammingError here poisons the whole asyncpg
            # connection and the subsequent INSERT INTO scraped_courses also
            # fails with "current transaction is aborted".
            #
            # auto_add=False — the staging transaction may still roll back
            # later, so we must not INSERT new taxonomy rows here.
            async with db.begin_nested():
                canonical = await resolve_sub_category(
                    db, _cat_for_match, _sub_for_match, auto_add=False,
                )
            if canonical and canonical != _sub_for_match:
                log.info(
                    "stage_course: snapped sub_category %r → %r for %r (cat=%s)",
                    _sub_for_match, canonical, name, _cat_for_match,
                )
                payload["sub_category"] = canonical
        except Exception as exc:  # noqa: BLE001 — never let canonicalisation abort staging
            log.warning(
                "stage_course: sub_category canonicalisation failed for %r: %s",
                name, exc,
            )

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

    # Fallback degree_level inference from course_name.
    #
    # The degree_level extractor in single_course.py uses payload.setdefault()
    # so it can be silently blocked when something upstream set degree_level=None
    # explicitly (e.g. a sibling-cache hit that returned None, or a pre-seed).
    # This staging chokepoint runs AFTER all extractors and acts as a guaranteed
    # last-resort: if degree_level is still blank, re-derive it from course_name
    # using the same classifier.  This catches qualifications like MA, MPH, LLM,
    # LLB, BNurs, BMid, MPharm, HNC, HND, FdA/FdSc that the extractor resolved
    # but whose result was blocked by a prior None assignment.
    if not (payload.get("degree_level") or "").strip():
        _cname_for_dl = (payload.get("course_name") or "").strip()
        if _cname_for_dl:
            try:
                from app.services.scraper.extractors.degree_level import (
                    classify_degree_level as _classify_dl,
                )
                _inferred_dl, _dl_method, _ = _classify_dl(_cname_for_dl)
                if _inferred_dl:
                    payload = dict(payload)
                    payload["degree_level"] = _inferred_dl
                    log.info(
                        "[DL FALLBACK] %s: %r → %r (method=%s)",
                        name, _cname_for_dl[:60], _inferred_dl, _dl_method,
                    )
            except Exception:  # noqa: BLE001
                pass  # never block staging on inference failure

    # Universal "Online" / virtual-mode scrub for course_location.
    #
    # Rationale: the location extractor cascade calls _sanitise_for_display
    # (which strips Online via _REMOVE_VIRTUAL), but Online still leaks into
    # course_location via at least three other paths:
    #   1. AI fallback (gemini_primary / ai_fallback) writes location_text /
    #      course_location values that pass page-text validation because
    #      "Online" appears verbatim on Federation / Mt-Helen-style pages.
    #   2. Direct uni-specific writes (Bond / ECU / CSU) bypass the
    #      cascade entirely and assign payload["course_location"] directly.
    #   3. Central PDF / fee-page extractors that join campus tokens with
    #      ", " around an Online row.
    # Online is a delivery mode, not a campus, so it must never appear in
    # course_location regardless of source.  Scrubbing here is the single
    # chokepoint every staged row passes through.
    _raw_loc = (payload.get("course_location") or "").strip()
    if _raw_loc:
        try:
            from app.services.scraper.extractors.location import _REMOVE_VIRTUAL
            parts = [p.strip() for p in _raw_loc.split(",") if p.strip()]
            kept = [p for p in parts if not _REMOVE_VIRTUAL.search(p)]
            if len(kept) != len(parts):
                payload = dict(payload)
                payload["course_location"] = ", ".join(kept) if kept else None
                log.info(
                    "[LOC SCRUB] %s: %r → %r",
                    name, _raw_loc, payload["course_location"],
                )
        except Exception:  # noqa: BLE001
            pass  # never block staging on scrub failure

    # 2026-05-13: Defensive trailing-country strip for course_location.
    # The structural cascade in extractors/location.py no longer appends
    # ", Australia" / ", New Zealand" (per user preference — every uni in
    # this system is AU/NZ so the country tag is noise), and the cascade's
    # _sanitise_for_display strips bare country tokens.  But other paths
    # (Gemini fallback location_text, label-derived strong-tag captures
    # off VU's "Sydney, Melbourne, Brisbane, Australia" footer-style
    # campus list, central fee-page joins, ECU static extract) can still
    # leak a trailing country word past staging.  This is the single
    # chokepoint every staged row passes through, so strip here too.
    _raw_loc2 = (payload.get("course_location") or "").strip()
    if _raw_loc2:
        _stripped = re.sub(
            r"\s*,\s*(?:Australia|New Zealand|NZ)\s*$",
            "",
            _raw_loc2,
            flags=re.IGNORECASE,
        ).strip(" ,")
        if _stripped != _raw_loc2:
            payload = dict(payload)
            payload["course_location"] = _stripped if _stripped else None
            log.info(
                "[LOC COUNTRY STRIP] %s: %r → %r",
                name, _raw_loc2, payload["course_location"],
            )

    # Strip institutional label prefixes and reject bare delivery-mode labels.
    #
    # Some CMSes (Wolverhampton, etc.) prefix every campus with "University:"
    # and Gemini faithfully copies it into location_text.  The fix in
    # _sanitise_for_display covers new scrapes via the Gemini path, but this
    # staging chokepoint handles ALL paths (structural extractor, AI fallback,
    # uni-specific pre-seeds) and also cleans already-staged rows on re-scrape.
    #
    # Examples cleaned here:
    #   "University: City Campus"  → "City Campus"
    #   "University: City Campus, University: Springfield Campus"
    #                              → "City Campus, Springfield Campus"
    #   "University:"              → None  (bare label → drop)
    #   "Mode"                     → None  (delivery-mode label → drop)
    _raw_loc3 = (payload.get("course_location") or "").strip()
    if _raw_loc3:
        try:
            from app.services.scraper.extractors.location import (
                _INST_LABEL_PREFIX_RE as _ilpre,
                _is_only_delivery_method as _iodm,
            )
            _loc3_parts = [
                _ilpre.sub("", p).strip()
                for p in _raw_loc3.split(",")
            ]
            _loc3_parts = [p for p in _loc3_parts if p and not _iodm(p)]
            _loc3 = ", ".join(_loc3_parts) if _loc3_parts else None
            if _loc3 != _raw_loc3:
                payload = dict(payload)
                payload["course_location"] = _loc3
                log.info(
                    "[LOC PREFIX STRIP] %s: %r → %r",
                    name, _raw_loc3, _loc3,
                )
        except Exception:  # noqa: BLE001
            pass  # never block staging on clean failure

    # ── Per-uni global_substring_blocklist + field_overrides (opt-in) ────────
    # Two YAML knobs applied here so they affect EVERY string field on the
    # final payload regardless of which extractor wrote it:
    #   text_cleaning.global_substring_blocklist  → strip boilerplate
    #   text_cleaning.field_overrides             → URL-regex hard overrides
    # Both are no-ops when the lists are empty (default).
    try:
        from app.services.scraper.config.context import get_uni_config
        _cfg = get_uni_config()
    except Exception:  # noqa: BLE001
        _cfg = None
    if _cfg is not None:
        # 1) global substring blocklist (case-insensitive)
        _blk = [s for s in (_cfg.extraction.text_cleaning.global_substring_blocklist or []) if s and s.strip()]
        if _blk:
            payload = dict(payload)
            for _fld, _val in list(payload.items()):
                if not isinstance(_val, str) or not _val:
                    continue
                _new = _val
                for _sub in _blk:
                    if not _sub:
                        continue
                    _new = re.sub(re.escape(_sub), "", _new, flags=re.IGNORECASE)
                _new = re.sub(r"\s{2,}", " ", _new).strip(" ,;:-\t\n")
                if _new != _val:
                    payload[_fld] = _new if _new else None
                    log.info(
                        "[TEXT BLOCKLIST] %s.%s: %r → %r",
                        name, _fld, _val[:80], (_new[:80] if _new else None),
                    )
        # 2) field_overrides (per-URL regex hard sets)
        _fos = list(_cfg.extraction.text_cleaning.field_overrides or [])
        if _fos:
            _course_url = (payload.get("course_website") or payload.get("source_url") or "").strip()
            if _course_url:
                payload = dict(payload)
                for _fo in _fos:
                    try:
                        if re.search(_fo.url_regex, _course_url, re.IGNORECASE):
                            _old = payload.get(_fo.field)
                            _new_val = _fo.value if (_fo.value is not None and _fo.value != "") else None
                            payload[_fo.field] = _new_val
                            log.info(
                                "[FIELD OVERRIDE] %s.%s: %r → %r (url=%s, regex=%s)",
                                name, _fo.field, _old, _new_val, _course_url, _fo.url_regex,
                            )
                    except re.error:
                        log.warning("field_overrides: invalid regex skipped: %s", _fo.url_regex)

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

    # ----- Phase 9: field verification engine -----
    # Runs after evidence is flushed; computes cross-source agreement-based
    # confidence and writes to field_verification_results. Also sets
    # avg_verification_confidence on the staged row so the auto-publish gate
    # can use it without an extra join. Wrapped in try/except — never blocks staging.
    if evidence_count:
        try:
            from app.services.scraper.verification_engine import run_field_verification

            v_summary = await run_field_verification(db, sc.id)
            avg_vc = v_summary.get("avg_confidence")
            if avg_vc is not None:
                sc.avg_verification_confidence = avg_vc
                # Re-evaluate auto_publish with the new confidence gate
                ap2 = should_auto_publish(sc)
                sc.auto_publish_status = "ready" if ap2.auto_publish else "review"
                log.info(
                    "verification_engine sc=%s avg_confidence=%.1f fields=%d verified=%d conflicts=%d → auto_publish=%s",
                    sc.id, avg_vc,
                    v_summary.get("field_count", 0),
                    v_summary.get("verified_count", 0),
                    v_summary.get("conflict_count", 0),
                    sc.auto_publish_status,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "verification engine failed for sc %s (uni %s): %s",
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
