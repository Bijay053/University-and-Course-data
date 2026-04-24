"""Cross-evidence conflict detector.

Closes MIGRATION_AUDIT.md item I — the Node ``review-engine`` populated
``field_conflicts`` rows so the Review modal could render mismatch banners;
the Python pipeline persisted evidence but never wrote any conflict rows,
so the modal always rendered ``conflicts: []``.

Algorithm (kept deliberately small — the Node engine ranks candidates with
a 200-line scoring function; we only need to emit the rows the UI reads):

    1. Read all ``ScrapedFieldEvidence`` rows for the staged course.
    2. Group by ``field_key``.
    3. Within each group, find every distinct pair (a, b) where:
         * both rows have a non-empty ``normalized_value``,
         * the normalized values differ,
         * both rows come from the same evidence tier (see
           ``_evidence_tier``) — a course-page candidate disagreeing with
           a uni-PDF fallback isn't a conflict, it's expected, and the
           tier ordering already biases the Review UI toward the course
           page.
    4. Deduplicate by the unordered ``(value_a, value_b)`` pair so we don't
       write N² rows when one field has 5 candidates with the same two
       distinct values.
    5. Emit ``FieldConflict(conflict_type="source_mismatch")`` rows.

Idempotent: any pre-existing ``field_conflicts`` rows for the staged
course are deleted first so re-running the detector after evidence edits
gives a fresh, deduplicated set.
"""
from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FieldConflict, ScrapedFieldEvidence

log = logging.getLogger(__name__)


# Tier ordering — only candidates from the same tier can contradict each
# other. ``course_page`` is the gold standard, ``listing_page`` is one
# step down (often dropped fields), ``uni_pdf`` and ``ai_fallback`` are
# best-effort and don't earn the right to file a complaint against the
# course-page evidence.
_TIER_BY_PAGE_TYPE: dict[str, str] = {
    "course_page": "primary",
    "course": "primary",
    "course_pdf": "primary",
    "listing_page": "listing",
    "listing": "listing",
    "uni_pdf": "fallback",
    "fee_pdf": "fallback",
    "ai_fallback": "fallback",
    "ai": "fallback",
}

# extraction_method-based fallback. The Python pipeline doesn't always
# populate page_type on evidence rows (the extractors were written
# before the conflict detector existed), so we also bucket by method
# prefix. ``uni_pdf:fees`` / ``uni_pdf:requirements`` / ``ai_fallback``
# are the standard low-tier methods; anything else is treated as
# primary so we don't silently swallow same-tier disagreements from
# an extractor we forgot to catalogue.
_FALLBACK_METHOD_PREFIXES: tuple[str, ...] = (
    "uni_pdf",
    "ai_fallback",
    "ai:",
    "vision",
)


def _evidence_tier(ev: ScrapedFieldEvidence) -> str:
    """Map (page_type | extraction_method) → tier. Unknown sources fall
    into ``primary`` so we don't silently swallow conflicts from an
    extractor we forgot to catalogue."""
    pt = (ev.page_type or "").lower()
    if pt in _TIER_BY_PAGE_TYPE:
        return _TIER_BY_PAGE_TYPE[pt]
    method = (ev.extraction_method or "").lower()
    if any(method.startswith(p) for p in _FALLBACK_METHOD_PREFIXES):
        return "fallback"
    return "primary"


def _norm_for_compare(value: str | None) -> str | None:
    """Strip + lowercase for the equality check only — the raw values are
    still what we persist into ``value_a`` / ``value_b``."""
    if value is None:
        return None
    s = value.strip().lower()
    return s or None


def _detect_pairs(
    rows: Iterable[ScrapedFieldEvidence],
) -> list[tuple[ScrapedFieldEvidence, ScrapedFieldEvidence]]:
    """Return one ordered pair per distinct disagreement within ``rows``."""
    by_tier: dict[str, list[ScrapedFieldEvidence]] = {}
    for r in rows:
        if not _norm_for_compare(r.normalized_value):
            continue
        by_tier.setdefault(_evidence_tier(r), []).append(r)

    seen: set[frozenset[str]] = set()
    out: list[tuple[ScrapedFieldEvidence, ScrapedFieldEvidence]] = []
    for tier_rows in by_tier.values():
        if len(tier_rows) < 2:
            continue
        # Sort by confidence desc so the higher-confidence row tends to land
        # on the ``value_a`` side — keeps the UI consistent across runs.
        sorted_rows = sorted(
            tier_rows, key=lambda r: (r.confidence or 0.0), reverse=True
        )
        for i in range(len(sorted_rows)):
            for j in range(i + 1, len(sorted_rows)):
                a, b = sorted_rows[i], sorted_rows[j]
                na = _norm_for_compare(a.normalized_value)
                nb = _norm_for_compare(b.normalized_value)
                if na is None or nb is None or na == nb:
                    continue
                key = frozenset({na, nb})
                if key in seen:
                    continue
                seen.add(key)
                out.append((a, b))
    return out


async def detect_and_persist_conflicts(
    db: AsyncSession, scraped_course_id: int
) -> int:
    """Run the detector for one staged course; return the number of conflict
    rows written. Caller is responsible for committing the session — this
    function flushes but does not commit so it can participate in a larger
    transaction (the staging path uses this).

    Defensive contract: a malformed evidence row must not abort the run.
    The single ``try`` around the row loop logs and continues.
    """
    rows = (
        await db.execute(
            select(ScrapedFieldEvidence).where(
                ScrapedFieldEvidence.scraped_course_id == scraped_course_id
            )
        )
    ).scalars().all()

    # Idempotency: clear any prior open conflicts for this course before
    # writing the fresh batch. Resolved/closed conflicts are kept (they
    # represent operator decisions worth preserving).
    await db.execute(
        delete(FieldConflict).where(
            FieldConflict.scraped_course_id == scraped_course_id,
            FieldConflict.status == "open",
        )
    )

    by_field: dict[str, list[ScrapedFieldEvidence]] = {}
    for r in rows:
        if not r.field_key:
            continue
        by_field.setdefault(r.field_key, []).append(r)

    written = 0
    for field_key, group in by_field.items():
        try:
            pairs = _detect_pairs(group)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "conflict pair-detection failed for sc %s field %s: %s",
                scraped_course_id, field_key, exc,
            )
            continue
        for a, b in pairs:
            db.add(
                FieldConflict(
                    scraped_course_id=scraped_course_id,
                    field_key=field_key,
                    value_a=a.normalized_value,
                    value_b=b.normalized_value,
                    evidence_a_id=a.id,
                    evidence_b_id=b.id,
                    conflict_type="source_mismatch",
                    reason=(
                        f"{a.extraction_method or 'unknown'} vs "
                        f"{b.extraction_method or 'unknown'} disagreed"
                    ),
                    status="open",
                )
            )
            written += 1
    if written:
        await db.flush()
    return written
