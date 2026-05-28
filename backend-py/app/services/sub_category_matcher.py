"""Fuzzy sub-category matching + auto-add service.

Usage (inside an async SQLAlchemy session)::

    canonical = await resolve_sub_category(db, "Computer Science & IT", "Data Science")
    # → "Data Science & Big Data"  (closest match, ≥60% token overlap)

    canonical = await resolve_sub_category(db, "Computer Science & IT", "Quantum Computing")
    # → "Quantum Computing"  (new row inserted with auto_added=True)

The match pipeline:
1. Exact case-insensitive match → return existing name (no DB write).
2. Token-overlap match ≥ 60% among all sub-categories for the category
   → return the best existing name (no DB write).
3. No match → INSERT new row (auto_added=True) and return the raw value.
"""
from __future__ import annotations

import re
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sub_category import CourseSubCategory


# Common compound spellings that should be tokenised the same way as their
# split form so fuzzy matching against the canonical DB taxonomy works.
# Example: a Gemini guess of "Cybersecurity" must overlap with the canonical
# "Cyber Security" row (which tokenises to {cyber, security}).
_COMPOUND_SPLITS: dict[str, str] = {
    "cybersecurity": "cyber security",
    "biotechnology": "bio technology",
    "biomedical": "bio medical",
    "biochemistry": "bio chemistry",
    "ecommerce": "e commerce",
    "esports": "e sports",
    "fintech": "fin tech",
    "edtech": "ed tech",
    "healthtech": "health tech",
    "agritech": "agri tech",
    "infosec": "information security",
    "datasci": "data science",
}


def _tokens(text: str) -> set[str]:
    """Lowercase alphabetic tokens, 3+ chars, common stop-words removed.

    Compound spellings listed in :data:`_COMPOUND_SPLITS` are expanded into
    their constituent words BEFORE tokenisation so a Gemini guess of
    "Cybersecurity" matches the canonical "Cyber Security" row.
    """
    _STOP = {"and", "the", "for", "with", "of", "in", "to", "a", "an"}
    lowered = text.lower()
    for compound, expansion in _COMPOUND_SPLITS.items():
        if compound in lowered:
            lowered = lowered.replace(compound, expansion)
    return {
        w for w in re.findall(r"[a-z]{3,}", lowered)
        if w not in _STOP
    }


def _overlap(a: str, b: str) -> float:
    """Jaccard-like token overlap between two strings (0–1)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


async def resolve_sub_category(
    db: AsyncSession,
    category: str,
    raw_sub: str | None,
    *,
    threshold: float = 0.50,
    auto_add: bool = True,
) -> str | None:
    """Return the canonical sub-category name for *raw_sub* within *category*.

    Inserts a new row when no existing option matches well enough — unless
    *auto_add* is False, in which case the raw value is returned unchanged
    so the caller can decide what to do with it.

    Use ``auto_add=False`` at SCRAPE time (inside `stage_course`) where the
    surrounding transaction may still be rolled back: we only want to *snap*
    Gemini's free-text guess to an existing canonical row, never grow the
    taxonomy from a row that may not end up being staged.

    Use ``auto_add=True`` (the default) at APPROVE time where the row is
    definitely being persisted to the production `courses` table.

    Returns ``None`` if *raw_sub* is blank/None.
    """
    if not raw_sub or not raw_sub.strip():
        return None

    raw_sub = raw_sub.strip()

    rows = (
        await db.execute(
            select(CourseSubCategory.sub_category).where(
                CourseSubCategory.category == category
            )
        )
    ).scalars().all()

    # 1. Exact case-insensitive
    for existing in rows:
        if existing.lower() == raw_sub.lower():
            return existing

    # 2. Token-overlap fuzzy match
    best_score, best_name = 0.0, None
    for existing in rows:
        score = _overlap(raw_sub, existing)
        if score > best_score:
            best_score, best_name = score, existing

    if best_score >= threshold:
        return best_name

    # 3. No confident match. At scrape time we MUST NOT INSERT — the staging
    # transaction may still roll back, leaving an orphan taxonomy row. Just
    # return the raw value so it survives to approve time.
    if not auto_add:
        return raw_sub

    # 4. New — insert and return raw value
    db.add(CourseSubCategory(category=category, sub_category=raw_sub, auto_added=True))
    await db.flush()
    return raw_sub
