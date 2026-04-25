"""Course name extractor.

Best-effort: takes the first ``<h1>`` (then ``<title>``) and cleans it
the same way the Node ``preserveOriginalCapitalization`` helper does:
preserve standalone acronyms (MBA, BBA, ICT) and prepositions
(of/in/and/for) without lowercasing them. Strips trailing university or
campus suffixes ("- USQ", "| Charles Sturt University").
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.services.scraper.extractors.base import ExtractionResult

_PREPOSITIONS = {"of", "in", "and", "for", "the", "with", "to", "on", "by"}
_ACRONYMS = {
    "MBA", "BBA", "BA", "BS", "BSc", "MSc", "PhD", "ICT", "IT", "AI",
    "MD", "JD", "LLM", "LLB", "ME", "MEng", "EMBA", "GDip", "GCert",
    "MIT", "USQ", "CSU", "UTS", "ANU", "UNSW", "UoN", "RMIT",
    # Issue 3b: Roman numerals used in AQF course names (Certificate III,
    # Certificate IV, Diploma etc.). Without these, _smart_case title-cases
    # "III" as "Iii" and "IV" as "Iv".
    "II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII",
    "XIII", "XIV", "XV",
    # "V", "I", "X" are skipped — too likely to be the letter, not a numeral.
}
_TITLE_SUFFIX = re.compile(
    r"\s*[\|\-–—:•]\s*(?:[A-Z][A-Za-z& ]{1,40}\s+(?:University|College|Institute|Academy|School)|USQ|CSU|UTS|ANU|UNSW|RMIT|MIT|KBS)\s*$",
    re.I,
)
_DEGREE_QUAL_IN_TITLE_RE = re.compile(
    r"^\s*(?:master|bachelor|graduate|diploma|certificate|doctor|phd|mba\b|msc\b|bsc\b|bed\b)",
    re.I,
)
_NON_COURSE_PREFIX = re.compile(
    r"^\s*(?:home|study|courses?|programs?)\s*[/>\\:|–-]\s*", re.I
)
# Issue 3a: AQF code prefixes on VIT vocational course names.
# Pages set <h1>SIT40521 - Certificate IV in Kitchen Management</h1>.
# The code (3 uppercase letters + 5 digits) must be stripped before
# _smart_case runs, otherwise it becomes "Sit40521 - " in the output.
# Pattern matches: SIT40521 - , ICT40120 — , CPC30220 – etc.
_AQF_PREFIX_RE = re.compile(r"^[A-Za-z]{3}\d{5}\s*[-–—]\s*")


_SLUG_LIKE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){2,}$")


def _looks_like_slug(text: str) -> bool:
    """Detect URL-style slugs (``bachelor-of-business``).

    Triggered when the cleaned candidate is a single all-lowercase token
    with two or more hyphens — that pattern is unambiguous (real titles use
    spaces, not hyphens, between distinct words). One-hyphen survivors like
    ``co-op`` or ``part-time`` are deliberately *not* matched so we never
    mangle legitimate compound words.
    """
    return bool(_SLUG_LIKE.fullmatch(text.strip()))


def _unslug(text: str) -> str:
    """Replace hyphens with spaces so the slug can flow through ``_smart_case``."""
    return text.replace("-", " ")


def _smart_case(text: str) -> str:
    words = re.split(r"(\s+)", text.strip())
    out: list[str] = []
    for i, w in enumerate(words):
        if w.isspace() or not w:
            out.append(w)
            continue
        upper = w.upper().strip(",.;:()")
        bare = w.strip(",.;:()")
        if upper in _ACRONYMS:
            out.append(w.replace(bare, upper))
            continue
        lower = bare.lower()
        if i > 0 and lower in _PREPOSITIONS:
            out.append(w.replace(bare, lower))
            continue
        if bare and bare[0].isalpha():
            out.append(w.replace(bare, bare[0].upper() + bare[1:].lower()))
        else:
            out.append(w)
    return "".join(out)


def _clean(raw: str) -> str | None:
    if not raw:
        return None
    txt = re.sub(r"\s+", " ", raw).strip()
    # Issue 3a: strip AQF code prefix (e.g. "SIT40521 - ") before any
    # other processing so _smart_case never sees the raw code token.
    txt = _AQF_PREFIX_RE.sub("", txt).strip()
    txt = _NON_COURSE_PREFIX.sub("", txt)
    txt = _TITLE_SUFFIX.sub("", txt).strip(" -|·•")
    if not txt or len(txt) < 3 or len(txt) > 200:
        return None
    # Slug like "bachelor-of-business" → "Bachelor of Business". Done before
    # ``_smart_case`` so the prepositions/acronym rules apply uniformly.
    if _looks_like_slug(txt):
        txt = _unslug(txt)
    return _smart_case(txt)


async def extract(html: str, url: str) -> list[ExtractionResult]:  # noqa: ARG001
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[tuple[str, str, float]] = []
    h1 = soup.find("h1")
    if h1:
        candidates.append(("h1", h1.get_text(" ", strip=True), 0.9))
    title = soup.find("title")
    if title:
        candidates.append(("title", title.get_text(" ", strip=True), 0.6))

    # When both H1 and title are found: if the title (after cleaning) starts
    # with a degree qualifier (e.g. "MBA – ...") but the H1 does not, the page
    # is a specialisation sub-page where JS adds the parent degree prefix only
    # to the <title> (e.g. KBS MBA specialisations). Promote the title so the
    # full name like "MBA – Tourism and Hospitality Leadership" is used instead
    # of the bare "Tourism and Hospitality Leadership" from the H1.
    if len(candidates) == 2:
        h1_raw, title_raw = candidates[0][1], candidates[1][1]
        h1_clean = _clean(h1_raw) or ""
        title_clean = _clean(title_raw) or ""
        if (
            title_clean
            and _DEGREE_QUAL_IN_TITLE_RE.search(title_clean)
            and not _DEGREE_QUAL_IN_TITLE_RE.search(h1_clean)
        ):
            candidates = [("title", title_raw, 0.85), ("h1", h1_raw, 0.6)]

    for method, raw, conf in candidates:
        cleaned = _clean(raw)
        if cleaned:
            return [
                ExtractionResult(
                    field_key="course_name",
                    value=cleaned,
                    normalized={"course_name": cleaned},
                    confidence=conf,
                    method=f"course_name.{method}",
                    snippet=raw[:160],
                )
            ]
    return []
