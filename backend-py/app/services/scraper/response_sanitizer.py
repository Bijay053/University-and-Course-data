"""Response-level data guards for scraped-course API endpoints.

These mirrors of the staging guards (stage_course.py) ensure the review panel
and university detail tabs NEVER return dirty degree_level or course_location
values regardless of DB state (rows staged before guards existed).

Imported by:
  app/routers/reviews.py
  app/routers/universities.py
"""
from __future__ import annotations

import re

# ── Per-university strict location allowlists ─────────────────────────────────
# When a university_id appears here, ONLY these values (case-insensitive) are
# returned; anything else → None.  This is the primary guard.
# Generic fallback pattern-based filtering below is a secondary defence for
# universities NOT listed here.

_LOCATION_ALLOWLISTS: dict[int, frozenset[str]] = {
    # Birmingham City University — verified campus names only
    1760: frozenset({
        "City Centre", "City South", "Margaret Street",
        "Online", "Distance Learning", "UK Campus",
    }),
}


def _apply_allowlist(uni_id: int | None, raw: str | None) -> tuple[bool, str | None]:
    """If `uni_id` has an allowlist, enforce it and return (handled, value).

    Returns (True, cleaned_value) when an allowlist is in play.
    Returns (False, raw) when no allowlist is configured for this university
    so the caller falls through to the generic pattern-based filter.
    """
    if not uni_id or uni_id not in _LOCATION_ALLOWLISTS:
        return False, raw
    if not raw:
        return True, None
    allowed = _LOCATION_ALLOWLISTS[uni_id]
    # Accept if the whole value (stripped) exactly matches an allowlisted name
    stripped = raw.strip()
    if stripped in allowed:
        return True, stripped
    # Accept if every comma-split part individually matches
    parts = [p.strip() for p in stripped.split(",")]
    clean = [p for p in parts if p in allowed]
    return True, (", ".join(clean) if clean else None)


# ── Degree level ──────────────────────────────────────────────────────────────

CANONICAL_DL: frozenset[str] = frozenset({
    "Bachelor's", "Master's", "Doctorate",
    "Graduate Certificate", "Graduate Diploma",
    "Associate Degree", "Advanced Diploma",
    "Diploma", "Certificate", "Foundation",
})

_DL_CORRECTIONS: dict[str, str] = {
    "postgraduate certificate": "Graduate Certificate",
    "postgraduate diploma": "Graduate Diploma",
}

# ── Generic location garbage patterns (secondary defence) ────────────────────

_LOC_GARBAGE_PERSON_JOB = re.compile(
    r"\b(?:student|graduate|producer|presenter|speaker|professor|"
    r"doctor|director|lecturer|coordinator|researcher|alumni|"
    r"phd\s+student|course\s+leader|bbc|radio\s+\d|programme\s+lead|"
    r"course\s+director|engineer(?:ing)?\s+at|consultant\s+at|"
    r"reporter|journalist|artist|designer|developer|manager|"
    r"working\s+as)\b",
    re.IGNORECASE,
)
_LOC_GARBAGE_HONORIFIC = re.compile(
    r"^(?:dr|mr|ms|mrs|prof)\.?\s+[A-Za-z]",
    re.IGNORECASE,
)
_LOC_GARBAGE_PHRASES = re.compile(
    r"\b(?:please\s+note|worried\s+about|course\s+structure|"
    r"eu/international|credits\s+of|dissertation|one\s+of\s+our|"
    r"personal\s+statement|1xtra|radio\s+1|the\s+aims\s+of|"
    r"should\s+also\s+mention|now\s+working|current\s+phd|"
    r"ma\s+jewellery|trending\s+video)\b",
    re.IGNORECASE,
)
_LOC_DATE_RE = re.compile(
    r"(?:^|\s)(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+\d",
    re.IGNORECASE,
)

_GEO_WORDS = frozenset({
    "city", "centre", "center", "south", "north", "east", "west",
    "street", "road", "campus", "online", "distance", "learning",
    "margaret", "birmingham", "london", "manchester", "sheffield",
    "leeds", "bristol", "cardiff", "edinburgh", "glasgow", "oxford",
    "cambridge", "building", "floor", "hall", "house", "park",
    "central", "main", "new", "old", "upper", "lower", "international",
    "uk", "england", "wales", "scotland", "ireland",
})


def _is_person_name_guess(p: str) -> bool:
    """Return True if p looks like a bare person name with no campus context.

    Catches: 'Lauren Redfern', 'Jack Spencer', 'Ben Stones', 'Jocelyn Bennett',
    'Peter Latham', 'Aarushi', 'Hannah Wiggins', 'Emily Chesson', 'Zan Zver',
    'Seb Yates Cridland', 'Ava-Daniera McDonald', etc.
    """
    p_stripped = p.strip()
    words = p_stripped.split()
    if not words:
        return False
    # Single capitalised word that doesn't look like a place (e.g. "Aarushi", "Danielle")
    if len(words) == 1:
        w = words[0]
        if w.lower() in _GEO_WORDS:
            return False
        # All-caps abbreviations like "UK", "HND", "MBA" are not person names
        if w.isupper() and len(w) <= 5:
            return False
        return bool(re.match(r"^[A-Z][a-zA-Z'\-]{2,}$", w))
    # 2–4 word proper names
    if len(words) > 4:
        return False
    if any(w.lower() in _GEO_WORDS for w in words):
        return False
    # All words start with uppercase and contain letters/hyphens/apostrophes only
    return all(bool(re.match(r"^[A-Z][a-zA-Z'\-]+$", w)) for w in words)


def is_garbage_loc_part(p: str) -> bool:
    """Return True if this comma-split location part is non-campus garbage."""
    if len(p) > 80:
        return True
    if _LOC_DATE_RE.search(p):
        return True
    if _LOC_GARBAGE_HONORIFIC.search(p):
        return True
    if _LOC_GARBAGE_PERSON_JOB.search(p):
        return True
    if _LOC_GARBAGE_PHRASES.search(p):
        return True
    if re.match(r"^(?:one|some|many|all)\s+of\s*$", p, re.IGNORECASE):
        return True
    if re.search(r"[-–:]\s*$", p):
        return True
    if _is_person_name_guess(p):
        return True
    return False


def sanitize_location(raw: str | None, university_id: int | None = None) -> str | None:
    """Return a clean course_location value, enforcing allowlist when configured.

    For universities with a configured allowlist (e.g. BCU id=1760) the
    allowlist is the *only* gate — anything not in it becomes None.
    For all other universities a generic garbage-pattern filter is applied.
    """
    if not raw:
        return raw
    handled, value = _apply_allowlist(university_id, raw)
    if handled:
        return value
    # Generic pattern-based filter for universities without an allowlist
    parts = [p.strip() for p in raw.split(",")]
    clean = [p for p in parts if p and not is_garbage_loc_part(p)]
    return ", ".join(clean) if clean else None


def sanitize_degree_level(dl: str | None, course_name: str | None = None) -> str | None:
    """Return a canonical degree_level, re-inferring from course_name if needed."""
    if not dl:
        return dl
    dl_lower = dl.lower().strip()
    if dl_lower in _DL_CORRECTIONS:
        return _DL_CORRECTIONS[dl_lower]
    if dl in CANONICAL_DL:
        return dl
    try:
        from app.services.scraper.extractors.degree_level import classify_degree_level
        inferred, _, _ = classify_degree_level(course_name or dl)
        return inferred
    except Exception:
        return None


def sanitize_scraped_row(d: dict) -> dict:
    """Apply canonical-DL and location guards to a response dict in place.

    ``d`` must contain ``university_id`` so per-university allowlists are
    enforced.  Handles both ``course_location`` and ``location`` key aliases.
    """
    uni_id: int | None = d.get("university_id")

    d["degree_level"] = sanitize_degree_level(
        d.get("degree_level"), d.get("course_name")
    )
    if "course_location" in d:
        d["course_location"] = sanitize_location(d.get("course_location"), uni_id)
    if "location" in d:
        d["location"] = sanitize_location(d.get("location"), uni_id)
    return d
