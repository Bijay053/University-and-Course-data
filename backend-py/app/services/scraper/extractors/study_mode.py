"""Study-mode extractor (On Campus / Online / Blended / Mixed Mode).

Mirrors the Node implementation. The Review table renders the column from
``scraped_courses.study_mode``; without this extractor the column shows
"--" for every row.

Strategy is deliberately simple: scan the page text for any of a small
canonical vocabulary. Order of the patterns encodes precedence — Blended
beats On Campus when both are mentioned, because a course offered in both
modes is what "Blended" actually means.
"""
from __future__ import annotations

import re

from app.services.scraper.extractors.base import ExtractionResult

field_key = "study_mode"

# Higher-priority modes first (they imply on-campus + online both exist).
# Match Node's review-engine.ts vocabulary plus AU-specific phrasing.
# "On campus" includes the AU "onshore" idiom (CRICOS courses commonly
# describe overseas-student delivery as "Onshore - required to attend
# on campus"). "% online" appearing alongside any on-campus signal is a
# Blended marker even without the literal word "blended".
_MODE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(blended|hybrid|mixed[\s\-]?mode|on[\s\-]?campus\s+(?:and|&)\s+online|"
            r"online\s+(?:and|&)\s+on[\s\-]?campus)\b",
            re.IGNORECASE,
        ),
        "Blended",
    ),
    (
        re.compile(
            r"\b(fully\s+online|100%\s+online|online\s+(?:study|delivery|course|mode)|distance\s+learning|distance\s+education)\b",
            re.IGNORECASE,
        ),
        "Online",
    ),
    (
        re.compile(
            r"\b(on[\s\-]?campus|in[\s\-]?person|face[\s\-]?to[\s\-]?face|onshore|"
            r"required\s+to\s+attend\s+(?:on\s+)?campus|attend\s+on\s+campus)\b",
            re.IGNORECASE,
        ),
        "On Campus",
    ),
    # Plain "Online" mention as a fallback — only matches when the more
    # specific patterns above didn't fire. Kept last so e.g. "online and
    # on-campus" is still classified as Blended.
    (re.compile(r"\bonline\b", re.IGNORECASE), "Online"),
)

# Detects "X% online" / "up to X% online" anywhere in the text — paired
# with an on-campus signal, this means Blended (a course that's mostly
# in-person but officially permits some online study).
_PERCENT_ONLINE_RE = re.compile(
    r"\b(?:up\s+to\s+)?\d{1,3}\s*%\s+online\b", re.IGNORECASE
)
_ON_CAMPUS_RE = re.compile(
    r"\b(?:on[\s\-]?campus|onshore|attend\s+on\s+campus|in[\s\-]?person|face[\s\-]?to[\s\-]?face)\b",
    re.IGNORECASE,
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_tags(html: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html or ""))


def classify_study_mode(page_text: str) -> tuple[str | None, str | None]:
    """Return (study_mode, snippet).

    Order of operations:

    1. If the page text contains both an on-campus signal AND a "% online"
       phrase (e.g. "Onshore — required to attend on campus, allowed up to
       33% online") classify as Blended even when the literal word
       "blended" is absent. Mirrors Node's
       `review-engine.ts` heuristic — without it, courses with mixed
       delivery rules show as plain "On Campus" and the operator can't
       tell them apart from purely in-person courses.
    2. Fall through to the labelled pattern set (Blended → Online →
       On Campus → bare "Online").
    """
    plain = _strip_tags(page_text)

    pct = _PERCENT_ONLINE_RE.search(plain)
    if pct and _ON_CAMPUS_RE.search(plain):
        start = max(0, pct.start() - 60)
        return "Blended", plain[start : pct.end() + 60].strip()

    for pattern, label in _MODE_PATTERNS:
        m = pattern.search(plain)
        if m:
            start = max(0, m.start() - 30)
            return label, plain[start : m.end() + 30].strip()
    return None, None


async def extract(html: str, url: str) -> list[ExtractionResult]:
    mode, snippet = classify_study_mode(html)
    if not mode:
        return []
    return [
        ExtractionResult(
            field_key=field_key,
            value=mode,
            normalized={"study_mode": mode},
            confidence=0.7,
            method="study_mode:rule",
            snippet=snippet,
        )
    ]
