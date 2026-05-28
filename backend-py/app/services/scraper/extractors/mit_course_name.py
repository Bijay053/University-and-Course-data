"""Melbourne Institute of Technology (MIT) per-course title-major extractor.

MIT publishes 6 separate URLs for "Master of Networking" — one per major
specialisation (Project Management, Cloud Networking, Cybersecurity,
Smart Sensors / IoT, Cryptocurrency, plus the parent program).  The page
``<h1>`` on every one of those pages reads simply "Master of Networking",
so the standard h1-based course_name extractor produces 6 staged rows
with identical names.  The user-reported 2026-05-13 bug:

    Course Name             Score  Duration  Fee
    Master of Networking    85%    2 Year    A$11,640/Trimester
    Master of Networking    85%    2 Year    A$11,640/Trimester
    Master of Networking    85%    2 Year    A$11,640/Trimester
    Master of Networking    85%    2 Year    A$11,640/Trimester
    Master of Networking    85%    2 Year    A$11,640/Trimester
    Master of Networking    85%    2 Year    A$11,640/Trimester

The page ``<title>`` tag does carry the major, in one of three formats::

    Master of Networking | major in Project Management
    Master of Networking – major in Cloud Networking
    Bachelor of Business - Major in Accounting | Melbourne Institute of Technology

This extractor parses the title, detects the literal phrase "major in"
(case-insensitive, "majoring in" tolerated), pulls the major name out,
strips the trailing brand suffix ("| Melbourne Institute of Technology"),
and REPLACE-rewrites ``payload['course_name']`` to a canonical form::

    "<base degree> - Major in <Major Name>"

so each variant has a unique, parseable name.  The frontend splits on
" - Major in " to render the major on a second line beneath the base
degree.

Pages WITHOUT a "major in" marker (the parent ``master-networking`` page
whose title is "Master of Networking | Accredited IT Degree with
Industry Expertise") are left untouched so we never invent a major.

Hostname-gated on ``mit.edu.au``.  Pure parse — no extra HTTP request.

This extractor must run BEFORE ``mit_fees`` so the fee-table lookup can
exact-match on the now-fully-qualified course_name (the central MIT fee
table publishes the same "Bachelor of Business, major in Accounting"
canonical form).
"""
from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("uniportal.scraper.mit_course_name")


# ── Host gate ────────────────────────────────────────────────────────────
def is_mit_host(url: str | None) -> bool:
    """Strict netloc check — only ``mit.edu.au`` and its subdomains."""
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    if not host:
        return False
    return host == "mit.edu.au" or host.endswith(".mit.edu.au")


# ── Title parsing ────────────────────────────────────────────────────────
# Capture the inner text of the first ``<title>`` element.  MIT pages
# always emit a single non-empty title.
_TITLE_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.I)

# Trailing brand suffix variants seen in the wild: " | Melbourne
# Institute of Technology", " – Melbourne Institute of Technology", etc.
_BRAND_TAIL_RE = re.compile(
    r"\s*[|\-–—]\s*melbourne institute of technology\s*$",
    re.I,
)

# Marketing-tagline tail on the parent (no-major) page —
# "Master of Networking | Accredited IT Degree with Industry Expertise".
# Anything after a ``|`` that DOESN'T mention "major" is a tagline we
# strip when it's the only non-degree segment.
_MAJOR_MARKER_RE = re.compile(r"\bmajor(?:ing)?\s+in\s+", re.I)

# Separators used between base degree and major segment.
_SPLIT_RE = re.compile(r"\s*[|\-–—]\s*")


def _normalise_major(major: str) -> str:
    """Light cleanup of the major name: collapse whitespace and strip
    stray trailing punctuation.

    Casing is preserved as-published by MIT — every observed title in
    the wild already uses correct casing ("Project Management", "Cloud
    Networking", "Cybersecurity", "Smart Sensors and IoT", "Cyber
    Security", "Cryptocurrency").  Title-casing word-by-word would
    mangle stop-words ("and" → "And") and common acronyms (IoT → Iot),
    so we deliberately do nothing more than strip noise.
    """
    if not major:
        return ""
    s = re.sub(r"\s+", " ", unescape(major)).strip().strip(".,;:|")
    return s


def parse_title_major(title_text: str) -> tuple[str, str] | None:
    """Return (base_degree, major) parsed from a page <title> string.

    Returns ``None`` when the title doesn't contain a "major in" marker.

    Examples::

        "Master of Networking | major in Project Management"
            → ("Master of Networking", "Project Management")
        "Bachelor of Business - Major in Accounting | Melbourne Institute of Technology"
            → ("Bachelor of Business", "Accounting")
        "Master of Networking | Accredited IT Degree with Industry Expertise"
            → None  (no "major in" marker)
    """
    if not title_text:
        return None
    title = unescape(title_text).strip()
    # Strip the global brand suffix first so it doesn't end up in the
    # major capture.
    title = _BRAND_TAIL_RE.sub("", title).strip()
    m = _MAJOR_MARKER_RE.search(title)
    if not m:
        return None
    # Walk left from the marker to the nearest separator.  Anything
    # between that separator and the marker is the base degree.
    left_text = title[: m.start()]
    # The base degree sits in the segment immediately preceding the
    # separator that introduces the major segment — so split the left
    # half on separators and take the LAST segment.
    left_parts = [s.strip() for s in _SPLIT_RE.split(left_text) if s.strip()]
    if not left_parts:
        return None
    base = left_parts[-1].strip().strip(",.;:")
    # The major is everything after the marker, stopping at the next
    # separator (so trailing taglines like " | Melbourne …" are cut, but
    # we already stripped the brand suffix above).
    right_text = title[m.end():].strip()
    right_parts = [s.strip() for s in _SPLIT_RE.split(right_text) if s.strip()]
    if not right_parts:
        return None
    major = _normalise_major(right_parts[0])
    if not base or not major:
        return None
    return (base, major)


def extract_title_text(html: str) -> str | None:
    """Return the inner text of the first ``<title>`` tag, or None."""
    if not html:
        return None
    m = _TITLE_RE.search(html)
    if not m:
        return None
    return m.group(1).strip()


# ── URL-slug fallback (2026-05-14) ───────────────────────────────────────
# Bug observed in the wild: the Contemporary Management page
# (/programs/bachelor-business/contemporary-management) is missing both
# the standard <h1> AND the "major in" marker in its <title>.  Its
# title is ``"Bachelor of Business - Contemporary Management |
# Melbourne Institute of Technology"`` — just a hyphen, no marker.
# So ``parse_title_major`` correctly returns None, but a downstream
# extractor then writes the brand-suffixed title into payload, giving
# the user-reported ghost row::
#
#     "Bachelor of Business - Contemporary Management | Melbourne
#      Institute of Technology"
#
# The URL itself is the most reliable major signal.  Map the known
# base-degree URL prefixes to their canonical display names; everything
# after that prefix becomes the major slug.  We deliberately do NOT
# fire on /programs/<base>/ URLs without a trailing major segment
# (e.g. /programs/master-networking is the parent program page) so we
# never invent a major.

_BASE_DEGREE_PREFIXES: tuple[tuple[str, str], ...] = (
    # (URL prefix segment, canonical display name)
    # ``bachelor-business`` is the user-reported case (Contemporary
    # Management).  ``master-networking`` is whitelisted as a safety
    # net — its sub-pages currently work via the title-major path, so
    # the URL fallback only fires if that path ever fails (DOM shift).
    ("bachelor-business", "Bachelor of Business"),
    ("master-networking", "Master of Networking"),
)


def _slug_to_display(slug: str) -> str:
    """Convert a URL slug like ``"contemporary-management"`` into a
    display-cased major name like ``"Contemporary Management"``.

    Conservative casing: each token is title-cased except known stop
    words (``and``, ``of``, ``in``).  Matches the casing convention
    MIT uses on the working pages — see ``_normalise_major``.
    """
    if not slug:
        return ""
    _STOP = {"and", "of", "in", "the", "with", "for"}
    parts = [p for p in re.split(r"[-_]+", slug.strip()) if p]
    out: list[str] = []
    for i, p in enumerate(parts):
        lower = p.lower()
        if i > 0 and lower in _STOP:
            out.append(lower)
        else:
            out.append(p[:1].upper() + p[1:].lower())
    return " ".join(out)


def parse_url_major(url: str | None) -> tuple[str, str] | None:
    """Return (base_degree, major) parsed from a recognised MIT
    program URL, or None when the URL doesn't match a known
    base-degree-with-major pattern.

    Examples::

        /study-with-us/programs/bachelor-business/contemporary-management
            → ("Bachelor of Business", "Contemporary Management")
        /study-with-us/programs/bachelor-business/marketing-digital-communications
            → ("Bachelor of Business", "Marketing Digital Communications")
        /study-with-us/programs/bachelor-business
            → None  (no trailing major segment)
        /study-with-us/programs/master-networking/cyber-security
            → None  (master-networking is NOT in _BASE_DEGREE_PREFIXES;
                     master-networking pages are handled by the title path)
    """
    if not url:
        return None
    try:
        path = (urlparse(url).path or "").rstrip("/")
    except (ValueError, AttributeError):
        return None
    if not path:
        return None
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    last = parts[-1].lower()
    parent = parts[-2].lower() if len(parts) >= 2 else ""
    for prefix_slug, display_name in _BASE_DEGREE_PREFIXES:
        if parent == prefix_slug and last and last != prefix_slug:
            major = _slug_to_display(last)
            if major:
                return (display_name, major)
    return None


# ── Public entry-point ──────────────────────────────────────────────────
def apply_overrides(
    payload: dict[str, Any],
    html: str,
    *,
    url: str | None,
    evidence: list[dict],
) -> bool:
    """REPLACE ``payload['course_name']`` with
    ``"<base> - Major in <Major>"`` when the page title carries a
    "major in" marker.  Returns True if an override was applied.

    No-op when:
      - the host isn't MIT,
      - the page title has no "major in" marker (parent program page),
      - the parsed (base, major) cannot be cleanly extracted,
      - the new value matches the existing payload value (idempotent).
    """
    if not is_mit_host(url):
        return False
    title = extract_title_text(html or "")
    parsed: tuple[str, str] | None = None
    method = "mit_course_name:title_major"
    summary_extra = ""
    if title:
        parsed = parse_title_major(title)
        summary_extra = f"<title>={title!r}"
    if not parsed:
        # 2026-05-14 fallback — the Contemporary Management page (and
        # any future MIT page with the same shape) has no <h1>, no
        # "major in" marker in <title>, and a hyphen-separated title
        # that the bag-of-text extractor was writing back into
        # payload as ``"Bachelor of Business - Contemporary Management
        # | Melbourne Institute of Technology"``.  Derive the major
        # from the URL slug instead.
        url_parsed = parse_url_major(url)
        if url_parsed:
            parsed = url_parsed
            method = "mit_course_name:url_slug"
            summary_extra = f"url_path={urlparse(url).path!r}"
    if not parsed:
        return False
    base, major = parsed
    new_name = f"{base} - Major in {major}"
    prev_name = payload.get("course_name")
    if prev_name == new_name:
        return False
    payload["course_name"] = new_name
    evidence.append(
        {
            "field_key": "course_name",
            "method": method,
            "confidence": 0.95,
            "value": new_name,
            "source_url": url,
            "summary": f"{summary_extra}; was={prev_name!r}",
        }
    )
    log.info(
        "mit_course_name: course_name %r → %r [%s] (%s)",
        prev_name,
        new_name,
        method,
        url,
    )
    return True
