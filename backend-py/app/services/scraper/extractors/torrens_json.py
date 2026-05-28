"""Torrens University per-course JSON-LD location extractor.

Torrens University publishes a marketing-style Campus locations header on
EVERY course page that reads "Sydney, Melbourne, Brisbane, Adelaide,
Online" — the full set of campuses across the Torrens network — even
though most individual courses are only offered at a strict subset of
those campuses.  The visible-text location extractor picks up that
marketing header verbatim and stamps it onto every staged row, producing
the 2026-05-13 user-reported bug where 16+ Torrens courses (Education,
some Business specialisations, etc.) carried a 4-campus list when the
real availability is 1-3 campuses.

The page also embeds a ``<script type="application/ld+json">`` block of
type ``Course`` whose ``hasCourseInstance`` array carries the actual
per-campus availability, e.g.::

    "hasCourseInstance": [
      {"courseMode": "Online"},
      {"courseMode": "Onsite", "location": "Surry Hills campus"},
      {"courseMode": "Onsite", "location": "Flinders Street campus"}
    ]

That data is canonical (it's what Torrens themselves use to drive their
internal SPA) and is what we use here to REPLACE
``payload['course_location']``.

Hostname-gated (``is_torrens_host``) → no-op for every other uni.

Pattern follows ``federation_json``, ``cqu_json``, ``latrobe_json`` and
``mit_fees``: parse → normalise → REPLACE on a small set of authoritative
slots → record evidence → fail-soft so a parse error never breaks a
scrape.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("uniportal.scraper.torrens_json")


# ── Host gate ────────────────────────────────────────────────────────────
def is_torrens_host(url: str | None) -> bool:
    """Strict netloc check.  ``torrens.edu.au`` or any subdomain only."""
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    if not host:
        return False
    return host == "torrens.edu.au" or host.endswith(".torrens.edu.au")


# ── Campus → city map ────────────────────────────────────────────────────
# Built from a sweep of 12 Torrens course pages (2026-05-13). Keys are
# the bare campus slug (lower-cased, trailing " campus" stripped); values
# are the city we want surfaced in ``course_location``.
#
# Adding a new campus: keep this map small and explicit; do NOT regex
# infer city from campus name. If a future Torrens page introduces a new
# campus we don't know about, ``_campus_to_city`` returns the original
# label so the data isn't silently dropped — it just won't be city-
# normalised.  The unit tests intentionally include an "Unknown campus"
# case to lock that behaviour in.
_CAMPUS_TO_CITY: dict[str, str] = {
    # Sydney
    "surry hills": "Sydney",
    "ultimo": "Sydney",
    "pyrmont": "Sydney",
    # Melbourne
    "flinders street": "Melbourne",
    "fitzroy": "Melbourne",
    # Brisbane
    "fortitude valley": "Brisbane",
    # Adelaide
    "wakefield street": "Adelaide",
    # Blue Mountains (BMIHMS — Blue Mountains International Hotel
    # Management School operates under the Torrens network).
    "leura": "Blue Mountains",
}

# Canonical city display order — matches the order the Torrens marketing
# header uses, so visible UI doesn't reshuffle when we swap to JSON-LD.
_CITY_ORDER: tuple[str, ...] = (
    "Sydney",
    "Melbourne",
    "Brisbane",
    "Adelaide",
    "Blue Mountains",
)


def _campus_to_city(campus: str) -> str | None:
    """Map a JSON-LD ``location`` string to a city.

    Returns None for empty / non-string inputs.  Returns the original
    label (with trailing ``" campus"`` stripped) for campuses we don't
    have an explicit mapping for, so unknown new campuses surface as a
    raw label rather than getting silently dropped.
    """
    if not campus or not isinstance(campus, str):
        return None
    s = campus.strip()
    if not s:
        return None
    # Strip the literal trailing " campus" / " Campus" suffix.
    s_norm = re.sub(r"\s+campus$", "", s, flags=re.I).strip().lower()
    if not s_norm:
        return None
    return _CAMPUS_TO_CITY.get(s_norm, s.strip())


# ── JSON-LD parsing ──────────────────────────────────────────────────────
_JSONLD_BLOCK_RE = re.compile(
    r'<script[^>]*type=[\'"]application/ld\+json[\'"][^>]*>(.*?)</script>',
    re.S | re.I,
)


def extract_course_jsonld(html: str) -> dict[str, Any] | None:
    """Return the first ``@type=="Course"`` JSON-LD block, or None.

    Tolerates malformed JSON (returns None) and nested ``@graph`` arrays
    (walks one level deep). Pages with multiple Course blocks return the
    first one — every Torrens sample we inspected had exactly one.
    """
    if not html:
        return None
    for m in _JSONLD_BLOCK_RE.finditer(html):
        raw = m.group(1).strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        # Direct Course
        if isinstance(d, dict) and d.get("@type") == "Course":
            return d
        # @graph array — walk one level for a Course entry.
        if isinstance(d, dict) and isinstance(d.get("@graph"), list):
            for entry in d["@graph"]:
                if isinstance(entry, dict) and entry.get("@type") == "Course":
                    return entry
        # Top-level array of nodes.
        if isinstance(d, list):
            for entry in d:
                if isinstance(entry, dict) and entry.get("@type") == "Course":
                    return entry
    return None


def extract_campuses(course_block: dict[str, Any]) -> list[str]:
    """Return the deduped list of cities derived from ``hasCourseInstance``.

    Order: canonical city order (Sydney → Melbourne → Brisbane →
    Adelaide → Blue Mountains), with any unknown labels appended at the
    end in first-seen order so we don't lose them silently.
    """
    instances = course_block.get("hasCourseInstance") or []
    if not isinstance(instances, list):
        return []
    seen: dict[str, None] = {}
    for ci in instances:
        if not isinstance(ci, dict):
            continue
        loc = ci.get("location")
        # ``location`` can also be a dict like {"@type": "Place", "name": "..."}
        # in pure schema.org. Tolerate that too.
        if isinstance(loc, dict):
            loc = loc.get("name")
        city = _campus_to_city(loc) if isinstance(loc, str) else None
        if city:
            seen.setdefault(city, None)
    if not seen:
        return []
    # Sort by canonical order, then preserve insertion order for unknowns.
    canonical = [c for c in _CITY_ORDER if c in seen]
    extras = [c for c in seen if c not in _CITY_ORDER]
    return canonical + extras


# ── Public entry-point ──────────────────────────────────────────────────
def apply_overrides(
    payload: dict[str, Any],
    html: str,
    *,
    url: str | None,
    evidence: list[dict],
) -> bool:
    """REPLACE ``payload['course_location']`` from the JSON-LD Course
    block.

    Returns True if an override was applied, False otherwise.  Never
    raises — defensive parsing because a single malformed page must not
    poison a 200-course scrape job.
    """
    if not is_torrens_host(url):
        return False
    block = extract_course_jsonld(html)
    if not block:
        return False
    cities = extract_campuses(block)
    if not cities:
        return False
    new_loc = ", ".join(cities)
    prev_loc = payload.get("course_location")
    if prev_loc == new_loc:
        return False
    payload["course_location"] = new_loc
    evidence.append(
        {
            "field_key": "course_location",
            "method": "torrens_json:hascourseinstance",
            "confidence": 0.95,
            "value": new_loc,
            "source_url": url,
            "summary": (
                f"Torrens JSON-LD hasCourseInstance "
                f"{len(block.get('hasCourseInstance') or [])} entries "
                f"→ {len(cities)} unique cities; was={prev_loc!r}"
            ),
        }
    )
    log.info(
        "torrens_json: course_location %r → %r (%s)",
        prev_loc,
        new_loc,
        url,
    )
    return True
