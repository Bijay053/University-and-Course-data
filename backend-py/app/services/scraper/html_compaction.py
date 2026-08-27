"""Conservative course-page HTML compaction.

The generic extractor suite parses the same document many times.  Large CMS
pages can therefore spend seconds parsing navigation and footer chrome even
when the network request itself is fast.  This module removes only semantic,
high-confidence non-course containers and fails open whenever course signals
would be lost.
"""

from __future__ import annotations

import re
import time

from bs4 import BeautifulSoup
from app.services.html_compaction_counters import note_html_compaction

MIN_SOURCE_BYTES = 200_000
MIN_REDUCTION_RATIO = 0.10
_STRUCTURED_DESCENDANT_TAGS = (
    "script",
    "table",
    "form",
    "input",
    "select",
    "textarea",
    "template",
)

_NON_COURSE_ID_CLASS_RE = re.compile(
    r"(?:^|[-_ ])(?:"
    r"cookie(?:[-_ ](?:banner|consent|notice|preferences))?"
    r"|onetrust(?:[-_ ]banner)?"
    r"|usercentrics"
    r"|related[-_ ](?:courses|programmes|programs|degrees)"
    r"|recommended[-_ ](?:courses|programmes|programs|degrees)"
    r"|you[-_ ]may[-_ ]also[-_ ]like"
    r")(?:$|[-_ ])",
    re.IGNORECASE,
)

_CRITICAL_SIGNALS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bIELTS\b",
        r"\bCRICOS\b",
        r"\binternational\s+(?:tuition\s+)?fees?\b",
        r"\b(?:course\s+)?duration\b",
        r"\b(?:commencing|intake|start\s+date)s?\b",
        r"\bentry\s+requirements?\b",
    )
)

_VALUE_FINGERPRINT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # English-test score mentions, including sub-band values.
        r"\b(?:IELTS|PTE|TOEFL|Duolingo)\b[^<\n]{0,100}?\b\d{1,3}(?:\.\d+)?\b",
        # Currency-bearing fees.
        r"(?:\b(?:AUD|GBP|USD|NZD|EUR)\b|A?\$|£|€)\s*\d[\d,]*(?:\.\d+)?",
        # Australian registration codes.
        r"\bCRICOS\b[^<\n]{0,60}?\b[0-9]{5,6}[A-Z]\b",
        # Duration expressions and delivery-mode qualifiers.
        r"\b(?:duration|full[-\s]?time|part[-\s]?time|on[-\s]?campus|online)\b[^<\n]{0,80}",
        # Intake months. A month appearing only in removed chrome can otherwise
        # alter the generic intake extractor's first/union match.
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b",
        # Location/campus-labelled fragments.
        r"\b(?:location|campus)\b[^<\n]{0,100}",
    )
)


def _critical_signal_mask(html: str) -> tuple[bool, ...]:
    return tuple(bool(pattern.search(html)) for pattern in _CRITICAL_SIGNALS)


def _value_fingerprint(html: str) -> tuple[frozenset[str], ...]:
    return tuple(
        frozenset(" ".join(match.group(0).lower().split()) for match in pattern.finditer(html))
        for pattern in _VALUE_FINGERPRINT_PATTERNS
    )


def _matches_non_course_container(tag) -> bool:
    values = [tag.get("id") or ""]
    values.extend(tag.get("class") or [])
    return any(_NON_COURSE_ID_CLASS_RE.search(str(value)) for value in values)


def compact_course_html(
    html: str,
    *,
    enabled: bool = False,
    extra_remove_selectors: list[str] | None = None,
) -> str:
    """Return a smaller extraction document, or the original when unsafe.

    Scripts and structured data are intentionally retained.  A university can
    add selectors for known chrome through YAML; malformed selectors fail open.
    """
    if not enabled or not html:
        return html
    if len(html) < MIN_SOURCE_BYTES:
        note_html_compaction(
            outcome="skipped_small",
            input_bytes=len(html),
            output_bytes=len(html),
            elapsed_ms=0.0,
        )
        return html

    started = time.perf_counter()

    def fail_open(reason: str) -> str:
        note_html_compaction(
            outcome="fail_open",
            input_bytes=len(html),
            output_bytes=len(html),
            elapsed_ms=(time.perf_counter() - started) * 1000,
            reason=reason,
        )
        return html

    soup = BeautifulSoup(html, "html.parser")
    before_signals = _critical_signal_mask(html)
    before_values = _value_fingerprint(soup.get_text(" ", strip=True))
    h1_before = soup.find("h1")
    h1_text = h1_before.get_text(" ", strip=True) if h1_before else ""

    candidates = list(soup.find_all(["nav", "footer"]))
    candidates.extend(
        soup.find_all(attrs={"role": re.compile(r"^(?:navigation|contentinfo)$", re.I)})
    )
    candidates.extend(soup.find_all(_matches_non_course_container))

    for selector in extra_remove_selectors or []:
        try:
            candidates.extend(soup.select(selector))
        except Exception:
            return fail_open("invalid_selector")

    seen: set[int] = set()
    for tag in candidates:
        identity = id(tag)
        if identity in seen or tag.parent is None:
            continue
        seen.add(identity)
        if tag.name in _STRUCTURED_DESCENDANT_TAGS:
            # An extra selector directly targeting a structured element is an
            # unsafe configuration. Fail open for the entire document.
            return fail_open("structured_selector")
        # A semantic chrome container can still carry JSON-LD, CMS bootstrap
        # state, tables, or form-labelled values. Preserve it byte-for-byte
        # rather than trusting flattened-text parity for structural extractors.
        if tag.find(_STRUCTURED_DESCENDANT_TAGS) is not None:
            continue
        # Preserve visible text in its original document position so generic
        # first-match/union extractors see the same candidate values and order.
        # The speed win comes from collapsing thousands of nested chrome nodes
        # and attributes into one inert text container, not from deleting data.
        replacement = soup.new_tag("div")
        replacement["data-compacted-course-chrome"] = "true"
        replacement.string = " ".join(tag.stripped_strings)
        tag.replace_with(replacement)

    compacted = str(soup)
    if len(compacted) > len(html) * (1.0 - MIN_REDUCTION_RATIO):
        return fail_open("insufficient_reduction")
    if h1_text:
        h1_after = soup.find("h1")
        if h1_after is None or h1_after.get_text(" ", strip=True) != h1_text:
            return fail_open("heading_changed")
    after_signals = _critical_signal_mask(compacted)
    if any(was_present and not remains for was_present, remains in zip(before_signals, after_signals)):
        return fail_open("critical_signal_lost")
    if _value_fingerprint(soup.get_text(" ", strip=True)) != before_values:
        return fail_open("value_fingerprint_changed")
    note_html_compaction(
        outcome="accepted",
        input_bytes=len(html),
        output_bytes=len(compacted),
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )
    return compacted