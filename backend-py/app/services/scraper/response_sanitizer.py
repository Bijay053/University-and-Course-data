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

# ── Location garbage patterns (mirrors stage_course.py) ──────────────────────

_LOC_GARBAGE_PERSON_JOB = re.compile(
    r"\b(?:student|graduate|producer|presenter|speaker|professor|"
    r"doctor|director|lecturer|coordinator|researcher|alumni|"
    r"phd\s+student|course\s+leader|bbc|radio\s+\d|programme\s+lead|"
    r"course\s+director|engineer(?:ing)?\s+at|consultant\s+at)\b",
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
    r"should\s+also\s+mention)\b",
    re.IGNORECASE,
)
_LOC_DATE_RE = re.compile(
    r"(?:^|\s)(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+\d",
    re.IGNORECASE,
)
# Strings like "Rolex UK", "BAE Systems", "Britvic", "Cinesite" — company names
# after a comma in a person's job description
_LOC_COMPANY_AFTER_COMMA = re.compile(
    r",\s+(?:Rolex|BAE|Britvic|Cinesite|BBC|ITV|Sky|KPMG|Deloitte|PwC|EY|"
    r"NHS|Shell|BP|Rolls.Royce|Google|Microsoft|Amazon|Apple|Facebook|"
    r"Jaguar|Land\s+Rover|Barclays|HSBC|Lloyds|NatWest|Vodafone|BT|"
    r"Sony|Disney|Warner|Universal|Paramount)\b",
    re.IGNORECASE,
)


# Geographic / campus words that disqualify a string from being a person name.
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
    """Return True if p looks like a bare FirstName LastName with no campus context."""
    words = p.split()
    if len(words) < 2 or len(words) > 3:
        return False
    if any(w.lower() in _GEO_WORDS for w in words):
        return False
    # All words start with uppercase and contain only letters/hyphens/apostrophes
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
    if _LOC_COMPANY_AFTER_COMMA.search(p):
        return True
    if re.match(r"^(?:one|some|many|all)\s+of\s*$", p, re.IGNORECASE):
        return True
    # Bare trailing dash / colon — truncated sentence fragment
    if re.search(r"[-–:]\s*$", p):
        return True
    # Bare person name like "Lauren Redfern" or "Jack Spencer"
    if _is_person_name_guess(p):
        return True
    return False


def sanitize_location(raw: str | None) -> str | None:
    """Strip garbage text from course_location before sending to the UI."""
    if not raw:
        return raw
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
    # Non-canonical (e.g. full course name stored by mistake) — re-infer
    try:
        from app.services.scraper.extractors.degree_level import classify_degree_level
        inferred, _, _ = classify_degree_level(course_name or dl)
        return inferred
    except Exception:
        return None


def sanitize_scraped_row(d: dict) -> dict:
    """Apply canonical-DL and garbage-location guards to a response dict.

    Mutates `d` in place and returns it.  Handles both ``course_location``
    and ``location`` keys (universities.py recipe-preview endpoint uses both).
    """
    # Degree level
    d["degree_level"] = sanitize_degree_level(
        d.get("degree_level"), d.get("course_name")
    )
    # Course location (reviews endpoint)
    if "course_location" in d:
        d["course_location"] = sanitize_location(d.get("course_location"))
    # Location alias (recipe-preview endpoint in universities.py)
    if "location" in d:
        d["location"] = sanitize_location(d.get("location"))
    return d
