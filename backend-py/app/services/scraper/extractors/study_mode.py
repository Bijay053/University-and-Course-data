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
            # PR-1.5 prod regression: bare `\bblended\b` matched marketing
            # copy ("blended learning environment", "blended teaching
            # approach") on every VIT page and forced study_mode='Blended'
            # for all 24 staged courses. Require an explicit delivery
            # noun to follow the keyword (delivery / mode / format /
            # program / programme) so generic uses don't fire. Multi-mode
            # combos ("On Campus and Online") are still authoritative on
            # their own — those phrases only ever describe course delivery.
            r"\b(blended|hybrid|mixed[\s\-]?mode)[\s\-]+"
            r"(delivery|mode|format|program(?:me)?)\b|"
            r"\bon[\s\-]?campus\s+(?:and|&)\s+online\b|"
            r"\bonline\s+(?:and|&)\s+on[\s\-]?campus\b",
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
# B20: course pages frequently embed an enquiry-form `<select>` whose
# options literally read "Online Studies / On Campus / Blended". After
# tag-stripping that becomes a single line of prose with the word
# "Blended" in it, which the keyword fallback then claims as the
# course's mode. Same problem for `<form>` (other study-mode dropdowns)
# and `<nav>` / `<footer>` (site-wide navigation that lists every
# delivery option). Strip those *blocks entirely* — including their
# inner text — before we hand the HTML to the tag stripper.
_NOISE_BLOCK_RE = re.compile(
    r"<(select|form|nav|footer|aside)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)

# Authoritative label-style declarations. Almost every reputable course
# page surfaces the delivery mode as a key/value pair in the course
# summary card or on the dedicated info page — `Mode of study:`,
# `Study mode:`, `Delivery mode:`, `Mode of attendance:` etc. When a page
# emits one of these explicit labels we trust the value next to the
# label and skip the broad keyword scan entirely.
#
# This is the bug that caused 7 of 9 ASA prod rows to stage as `Online`:
# ASA's marketing copy mentions "online courses" / "online study options"
# in nav and footer text. The old fallback (Pattern 2 then bare-`online`
# Pattern 4) matched those phrases first and returned "Online" before
# checking the actual `Mode of study: On Campus` cell on the course page.
#
# Capture is *token-restricted*: we explicitly list the words that can
# appear inside a mode value (on/campus/online/blended/hybrid/and/&/...)
# and stop capturing as soon as we hit anything else. This matters
# because tag-stripping flattens `<dd>On Campus</dd><p>Study online...`
# into `On Campus Study online...` — without the token cap the value
# capture greedily grabs the next paragraph and triggers Blended via
# "online and on campus" appearing in unrelated marketing copy.
_MODE_TOKEN = (
    r"on[\s\-]?campus|online|blended|hybrid|mixed[\s\-]?mode|"
    r"distance(?:\s+(?:learning|education))?|in[\s\-]?person|"
    r"face[\s\-]?to[\s\-]?face|onshore|remote"
)
_MODE_JOINER = r"(?:\s+(?:and|or|&|/|,)\s+|\s*[/,]\s*)"
# Delimiter is REQUIRED (`:`, `-`, `–`). Without it the regex fires on
# unlabelled prose like "learn about mode of study online" and treats
# that as authoritative — code review caught this exact false positive.
# When a page uses a `<dt>Mode of study</dt><dd>On Campus</dd>` layout
# (no colon in the source) the existing bare-keyword pattern set still
# classifies "On Campus" correctly via the fallback path, so we don't
# lose coverage by requiring the delimiter here.
_LABEL_RE = re.compile(
    rf"\b(?:mode\s+of\s+(?:study|attendance|delivery|learning)|study\s+mode|"
    rf"delivery\s+mode|attendance\s+mode|study\s+method|"
    rf"learning\s+(?:mode|method)|delivery\s+method)\b\s*[:\-–]\s*"
    rf"((?:{_MODE_TOKEN})(?:{_MODE_JOINER}(?:{_MODE_TOKEN}))*)",
    re.IGNORECASE,
)

# Map a label *value* to a canonical study-mode label. Order matters —
# Blended must match before On Campus / Online so a value like
# "On Campus and Online" goes to Blended (the multi-mode case) rather
# than the first-keyword winner.
_VALUE_TO_LABEL: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"blended|hybrid|mixed", re.I), "Blended"),
    (re.compile(r"on[\s\-]?campus\s*(?:and|or|&|/|,)\s*online|online\s*(?:and|or|&|/|,)\s*on[\s\-]?campus", re.I), "Blended"),
    (re.compile(r"on[\s\-]?campus|in[\s\-]?person|face[\s\-]?to[\s\-]?face|onshore", re.I), "On Campus"),
    (re.compile(r"online|distance|remote", re.I), "Online"),
)


def _strip_tags(html: str) -> str:
    cleaned = _NOISE_BLOCK_RE.sub(" ", html or "")
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", cleaned))


def _classify_label_value(value: str) -> str | None:
    """Map the raw text after a `Mode of study:` label to a canonical
    label, or ``None`` when the value is gibberish (e.g. label was found
    but followed by an unrelated word in noisy HTML).
    """
    for pattern, label in _VALUE_TO_LABEL:
        if pattern.search(value):
            return label
    return None


def classify_study_mode(
    page_text: str,
) -> tuple[str | None, str | None, float | None]:
    """Return ``(study_mode, snippet, confidence)``.

    Order of operations:

    1. **Label detection first.** Scan for ``Mode of study:`` / ``Study
       mode:`` / ``Delivery mode:`` etc. and use the value next to the
       label as authoritative. This beats the broad keyword scan because
       course pages often mention "online" in unrelated copy (footer
       links, marketing) — without label-priority that bled into 7 of 9
       prod ASA rows showing as "Online".
    2. If the page text contains both an on-campus signal AND a "% online"
       phrase (e.g. "Onshore — required to attend on campus, allowed up to
       33% online") classify as Blended even when the literal word
       "blended" is absent. Mirrors Node's
       `review-engine.ts` heuristic — without it, courses with mixed
       delivery rules show as plain "On Campus" and the operator can't
       tell them apart from purely in-person courses.
    3. Fall through to the labelled pattern set (Blended → Online →
       On Campus → bare "Online").

    The third return is the confidence the extractor should attach to
    the result. Authoritative paths (label, percent-online + on-campus,
    explicit multi-keyword pattern) return 0.7. The bare-``\\bonline\\b``
    fallback (last entry of :data:`_MODE_PATTERNS`) returns 0.5 because
    it routinely fires on footer / marketing copy that mentions
    "online" in passing — keeping it low lets a more confident location
    or PDF signal override downstream. Returns
    ``(None, None, None)`` when no pattern matches.
    """
    plain = _strip_tags(page_text)

    label_match = _LABEL_RE.search(plain)
    if label_match:
        value = label_match.group(1).strip()
        canonical = _classify_label_value(value)
        if canonical:
            start = max(0, label_match.start() - 20)
            end = min(len(plain), label_match.end() + 20)
            return canonical, plain[start:end].strip(), 0.7

    pct = _PERCENT_ONLINE_RE.search(plain)
    if pct and _ON_CAMPUS_RE.search(plain):
        start = max(0, pct.start() - 60)
        return "Blended", plain[start : pct.end() + 60].strip(), 0.7

    last_idx = len(_MODE_PATTERNS) - 1
    for i, (pattern, label) in enumerate(_MODE_PATTERNS):
        m = pattern.search(plain)
        if m:
            start = max(0, m.start() - 30)
            # PR-5 Bug 2: bare-`\bonline\b` is the last pattern and is
            # the noisy one — drop confidence so location/PDF signals
            # outrank it during downstream merges.
            confidence = 0.5 if i == last_idx else 0.7
            return label, plain[start : m.end() + 30].strip(), confidence
    return None, None, None


async def extract(html: str, url: str) -> list[ExtractionResult]:
    mode, snippet, confidence = classify_study_mode(html)
    if not mode:
        return []
    return [
        ExtractionResult(
            field_key=field_key,
            value=mode,
            normalized={"study_mode": mode},
            confidence=confidence if confidence is not None else 0.7,
            method="study_mode:rule",
            snippet=snippet,
        )
    ]
