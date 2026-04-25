"""University-level pipeline: read fee + requirements PDFs ONCE per scrape job
and parse them with the same extractors used for course HTML pages.

Many universities (ASA, Torrens, …) publish their international tuition
schedule and admissions/IELTS policy as PDFs linked from the public site
rather than encoding the data on every course page. The per-course HTML
extractors will therefore find nothing and the resulting course rows are
empty for fee/IELTS even though the data exists in a known PDF.

This module:

1. Reads the URLs from ``university.scrape_config['uniPages']``:
   ``feesPdf``, ``requirementsPdf``.
2. Downloads + extracts text via :mod:`app.services.scraper.pdf_fetcher`.
3. Wraps the text as minimal HTML and runs the existing
   :func:`fee.extract` and :func:`english_test.extract` extractors so we
   share the regexes, currency detection, and IELTS sub-band logic
   instead of forking a parallel parser.
4. Returns a normalised payload that downstream callers merge into each
   course as a *last-resort* fallback (after page extractors + AI).
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.services.scraper.extractors import english_test, fee
from app.services.scraper.extractors.base import ExtractionResult
from app.services.scraper.pdf_fetcher import download_pdf_text
from app.services.scraper.pdf_vision import extract_via_vision

_VISION_TIMEOUT_S = 30.0
_VISION_MAX_BYTES = 12 * 1024 * 1024

log = logging.getLogger(__name__)


# Keys we will ever fill from a PDF. Anything outside this set stays
# course-page-only (course_name, location, intake, duration, eligibility).
_FEE_KEYS = ("international_fee", "currency", "fee_term", "fee_year")
_ENGLISH_KEYS = (
    "ielts_overall",
    "ielts_listening",
    "ielts_reading",
    "ielts_writing",
    "ielts_speaking",
    "pte_overall",
    "toefl_overall",
    "cambridge_overall",
    "duolingo_overall",
)


def _wrap_text_as_html(text: str) -> str:
    """Render plain text as a minimal HTML document.

    The existing extractors call :func:`html_to_text` first; <pre> keeps
    line breaks so currency/IELTS regexes that depend on whitespace
    proximity continue to work the same as on real pages.
    """
    # Escape only the bare minimum — the extractors only care about
    # textual content, not attributes.
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<html><body><pre>{safe}</pre></body></html>"


def _first_filled(results: list[ExtractionResult], keys: tuple[str, ...]) -> dict[str, Any]:
    """Take the highest-confidence (first-emitted) value per key from extractor results."""
    out: dict[str, Any] = {}
    for r in results:
        if not r.normalized:
            continue
        for k, v in r.normalized.items():
            if k not in keys or v is None:
                continue
            out.setdefault(k, v)
    return out


async def _download_raw_pdf(url: str) -> bytes:
    """Fetch the raw PDF bytes once so we can feed both ``pypdf`` and the
    vision-OCR fallback without two round-trips. Returns ``b""`` on any
    error — vision degrades to "no fallback" the same way."""
    if not url:
        return b""
    try:
        async with httpx.AsyncClient(
            timeout=_VISION_TIMEOUT_S, follow_redirects=True
        ) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                return b""
            content = r.content
            if len(content) > _VISION_MAX_BYTES:
                return b""
            # Light MIME guard — some uni sites return 200 + HTML when the
            # PDF link is broken.
            ct = (r.headers.get("content-type") or "").lower()
            if "pdf" not in ct and not content.startswith(b"%PDF"):
                return b""
            return content
    except Exception as exc:  # noqa: BLE001
        log.debug("_download_raw_pdf failed for %s: %s", url, exc)
        return b""


async def _vision_fallback_text(pdf_bytes: bytes, kind: str, url: str, emit) -> str:
    """Render a PDF and ask Gemini Vision to dump its facts as text.

    ``kind`` is "fee" or "requirements" — used only for the verbose log
    line. Returns "" when vision is disabled, fails, or yields nothing."""
    if not pdf_bytes:
        return ""
    if emit:
        await emit(
            "status",
            f"[FALLBACK] vision OCR on {kind} PDF: {url}",
            phase="extract",
            kind="pdf_vision_start",
        )
    text = await extract_via_vision(pdf_bytes)
    if emit:
        msg = (
            f"[FALLBACK] vision OCR {kind} PDF returned {len(text)} chars"
            if text
            else f"[FALLBACK] vision OCR {kind} PDF returned nothing (skipped or empty)"
        )
        await emit(
            "status",
            msg,
            phase="extract",
            kind="pdf_vision_done",
            chars=len(text),
        )
    return text


_AMOUNT_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")
_FEE_YEAR_RE = re.compile(r"\b(20\d{2})\b")


def _pick_amounts_from_pdf_text(text: str) -> dict[str, Any]:
    """Port of Node's ``pickAmounts`` heuristic for fee PDFs.

    Rationale (Bug G): the single-page ``fee.extract`` extractor scores
    candidates by proximity to a tuition cue word ("international", "fee",
    "tuition"), then returns the single best-scoring candidate. That works
    on web pages — but fee PDFs typically lay tuition out as a multi-row
    table where ALL the candidates appear next to the same cue, so the
    scorer misses the obvious signal: the LARGEST amount in the document
    is the full-course (international) tuition, not a per-trimester
    instalment. Result: prod was reporting per-trimester or per-unit fees
    as the international fee.

    This helper mirrors Node's ``extractFeesFromPdf``:

    * Dedupe all ``$X,XXX[.XX]`` amounts in the 1k–200k window.
    * Pick the max.
    * Mark as "Full Course" when there are ≥3 unique amounts, OR the max
      is ≥1.4× the next-largest, OR the text mentions "full course".
    * Per Unit when "per unit" appears anywhere.
    * Else "Annual".
    * Currency hard-coded to AUD (matches Node — every uni in scope is AU).
    """
    amounts: list[int] = []
    for m in _AMOUNT_RE.finditer(text):
        try:
            n = round(float(m.group(1).replace(",", "")))
        except ValueError:
            continue
        if 1000 < n < 200000:
            amounts.append(n)
    if not amounts:
        return {}

    unique = sorted(set(amounts))
    # Architect-flagged edge case: a single ``$5,000`` deposit alongside
    # an ``AUD 25,000`` tuition (no $ sign) used to short-circuit the
    # cue-aware ``fee.extract`` and return the deposit. With only one
    # ``$`` amount in the document we have no signal that it is
    # tuition vs. a deposit / textbook fee / scholarship value, so
    # we defer to ``fee.extract`` (which reads ``AUD 25,000`` style
    # amounts too) by returning empty. Two or more candidates is the
    # "real fee table" signal that justifies short-circuiting.
    if len(unique) < 2:
        return {}

    chosen = unique[-1]
    next_largest = unique[-2] if len(unique) > 1 else None

    looks_like_full_course = (
        len(unique) >= 3
        or (next_largest is not None and chosen >= next_largest * 1.4)
        or bool(re.search(r"\bfull\s+course\b", text, re.I))
    )

    if looks_like_full_course:
        term = "Full Course"
    elif re.search(r"\bper\s+unit\b", text, re.I):
        term = "Per Unit"
    else:
        term = "Annual"

    out: dict[str, Any] = {
        "international_fee": chosen,
        "currency": "AUD",
        "fee_term": term,
    }
    year_match = _FEE_YEAR_RE.search(text)
    if year_match:
        out["fee_year"] = int(year_match.group(1))
    return out


# ---------------------------------------------------------------------------
# Per-course PDF table parsing
# ---------------------------------------------------------------------------
#
# The uni-wide ``_pick_amounts_from_pdf_text`` above treats the fee schedule
# as a *single* number — fine when a uni publishes one tuition figure for
# everyone, broken when the schedule is a per-course table (ASA, Torrens,
# etc.) where every course has its own row. Stamping the max amount on
# every course is exactly the "uniform fee across siblings" failure mode
# we keep hitting.
#
# This block adds a per-row table parser. A "data row" looks like:
#
#   <CRICOS-code> <years|"6 Months"> <units>[*] $<per-unit> $<annual> $<total>
#
# e.g. ``117606J 2 15* $3,300 $26,400 $52,800``. The course name is the
# preceding non-empty, non-header lines (handles multi-line names like
# "Bachelor of Business" + "Including Majors:" + a list of majors).
#
# Output: ``{ normalized_primary_name: {international_fee, currency,
# fee_term, fee_year, _pdf_match_text, _cricos} }``. Downstream callers
# match a course by name with :func:`match_course_in_pdf_table` and use the
# matched row IN PREFERENCE TO the uni-wide value.

_PDF_DATA_ROW_RE = re.compile(
    # CRICOS course codes are 6 digits + 1 trailing letter (e.g. 102219K,
    # 117606J). Anchoring with \b on both sides avoids matching unit
    # counts ("15*") or fee-amount digits ("19,360"). NOT line-anchored
    # because pypdf often concatenates a course name and its data row
    # onto a single line ("Diploma of Business  108861B 1 8 …"), so
    # requiring ^\s* would silently skip those rows.
    r"\b(?P<cricos>\d{6}[A-Z])\b\s+"
    r"(?P<duration>\d+(?:\s*Months?)?)\s+"
    r"(?P<units>\d+\*?)\s+"
    r"\$(?P<per_unit>[\d,]+)\s+"
    r"\$(?P<annual>[\d,]+)\s+"
    r"\$(?P<total>[\d,]+)",
)

# A "name line" is one that starts with a degree-level word. Continuation
# lines (parentheticals like "(Cyber Security)", "Including Majors:",
# major sub-lists) are folded into the most recent name line.
#
# NOTE: ``Undergraduate`` / ``Postgraduate`` are deliberately excluded —
# they appear in fee schedules as section dividers, not as part of any
# course name. Including them would cause the extractor to lock onto
# "Undergraduate" as the primary name and drop the real one.
_PDF_DEGREE_LEAD_RE = re.compile(
    r"^\s*(bachelor|master|graduate|"
    r"diploma|associate|doctor|doctorate|certificate|honours|honors)\b",
    re.I,
)

# Tokens we drop when normalizing a course name for matching. They are
# either grammatical filler ("of", "the") or degree-level boilerplate
# ("master", "bachelor") that appears in too many courses to be a useful
# discriminator.
_NAME_STOPWORDS = {
    "of", "the", "and", "in", "for", "to", "a", "an", "with",
    "bachelor", "master", "graduate", "postgraduate", "undergraduate",
    "certificate", "diploma", "associate", "doctor", "doctorate",
    "honours", "honors", "degree",
}


def _name_tokens(name: str) -> set[str]:
    """Lowercase, strip punctuation, drop stopwords + degree-level words.

    Used by :func:`match_course_in_pdf_table` to compute overlap between
    a DB course name (e.g. "Master of Information Technology (Software
    Application Development)") and a PDF row name (e.g. "Master of
    Software Application Design"). Returning a *set* of distinctive
    tokens — not a full string — keeps the matcher stable against
    parenthetical reorderings and minor wording differences.
    """
    if not name:
        return set()
    lowered = name.lower()
    # Replace punctuation with spaces, then collapse.
    cleaned = re.sub(r"[^a-z0-9\s]+", " ", lowered)
    return {tok for tok in cleaned.split() if tok and tok not in _NAME_STOPWORDS}


# Canonical degree-level prefixes, longest-first so multi-word titles
# ("Graduate Certificate", "Associate Degree") are tried before their
# single-word substrings. Used by :func:`_degree_level` to give the
# matcher a hard award-level filter — without it, "Master of Public
# Health" and "Graduate Certificate of Public Health" tokenise to
# the same set ``{public, health}`` after stopword stripping and the
# matcher cannot tell them apart even when they're separate CRICOS
# rows in the schedule. (PR-7 review finding.)
_DEGREE_LEVEL_PREFIXES: list[tuple[str, str]] = [
    ("graduate certificate", "graduate-certificate"),
    ("graduate diploma", "graduate-diploma"),
    ("postgraduate certificate", "graduate-certificate"),
    ("postgraduate diploma", "graduate-diploma"),
    ("advanced diploma", "advanced-diploma"),
    ("associate degree", "associate"),
    ("associate diploma", "associate"),
    ("associate", "associate"),
    ("master", "master"),
    ("bachelor", "bachelor"),
    ("honours", "bachelor"),
    ("honors", "bachelor"),
    ("doctorate", "doctor"),
    ("doctor", "doctor"),
    ("diploma", "diploma"),
    ("certificate", "certificate"),
]


def _degree_level(name: str) -> str:
    """Return a canonical award-level token, or '' if undetected.

    Examples::

        "Master of Public Health"               -> "master"
        "Graduate Certificate of Public Health" -> "graduate-certificate"
        "Bachelor of Business (Honours)"        -> "bachelor"
        "Doctor of Philosophy"                  -> "doctor"
        "Diploma of Nursing"                    -> "diploma"
        "Foundation Studies"                    -> ""

    Used by :func:`match_course_in_pdf_table` to drop candidate rows
    whose award level disagrees with the DB course's award level —
    prevents Cert/Diploma/Master cross-matching when only the stem
    overlaps.
    """
    if not name:
        return ""
    s = name.strip().lower()
    for prefix, canonical in _DEGREE_LEVEL_PREFIXES:
        if s.startswith(prefix):
            return canonical
    return ""


def _extract_primary_name(name_block: str) -> tuple[str, str]:
    """Pull the actual course title out of the text preceding a data row.

    The name region given to us can contain the document title, table
    headers, level dividers ("Undergraduate"), footnote tails from the
    previous row, and the new course's name itself — possibly split over
    multiple lines (``Master of Information Technology`` / ``(Cyber
    Security)``).

    Strategy: walk lines in order; ignore everything until we hit a line
    that starts with a degree-level word (Bachelor, Master, …); then keep
    accumulating *parenthetical/continuation* lines (e.g. ``(Cyber
    Security)``) into the **primary name**. Treat ``Including Majors:``
    and the major sub-list lines that follow as **extras** — they
    enrich the matcher's token bag (so a DB course named "Bachelor of
    Business Hospitality Management" matches the row that's only labeled
    "Bachelor of Business" in the PDF) but stay out of the visible
    primary name.

    Returns ``(primary, extras)`` where ``primary`` is the visible
    course title (e.g. "Master of Information Technology (Cyber
    Security)") and ``extras`` is extra text used only by the matcher.
    Empty primary means we'll skip this row.
    """
    primary_parts: list[str] = []
    extras_parts: list[str] = []
    started = False
    in_majors = False  # set after we see "Including Majors:" or similar

    for raw in name_block.splitlines():
        s = raw.strip()
        if not s:
            continue
        # Footnote lines like "*Final unit within the course is worth…"
        # always belong to the *previous* row, never the next one.
        if s.startswith("*"):
            continue
        if _PDF_DEGREE_LEAD_RE.match(s):
            if started:
                # Second degree-lead line → previous course's name was
                # already complete; the rest of this block belongs to
                # the next data row, not this one.
                break
            primary_parts = [s]
            started = True
            in_majors = False
            continue
        if not started:
            continue
        # Once we hit "Including Majors:" everything after is sub-majors,
        # which feed the token bag but not the primary name.
        if s.lower().startswith("including majors") or s.lower().startswith(
            "including specialisations"
        ):
            in_majors = True
            extras_parts.append(s)
            continue
        if in_majors:
            extras_parts.append(s)
        else:
            primary_parts.append(s)

    primary = " ".join(primary_parts).strip()
    extras = " ".join(extras_parts).strip()
    return primary, extras


def _pick_per_course_amounts(text: str) -> dict[str, dict[str, Any]]:
    """Parse a per-course tuition table out of a fee-schedule PDF.

    Returns ``{normalized_primary_name: row_dict}`` where each ``row_dict``
    has the same shape as :func:`_pick_amounts_from_pdf_text` (so the
    downstream merge code is uniform), plus two private fields
    (``_pdf_match_text`` and ``_cricos``) used by the matcher.

    Returns ``{}`` when the document doesn't contain at least 2 data rows
    — a single row is more likely a misdetection than a real per-course
    table, and the existing uni-wide path already handles single-fee
    documents.
    """
    if not text:
        return {}

    # Collect the matches AND track end-positions so we can slice out the
    # text BEFORE each row as the candidate course name.
    matches = list(_PDF_DATA_ROW_RE.finditer(text))
    if len(matches) < 2:
        return {}

    year_default = None
    year_match = _FEE_YEAR_RE.search(text)
    if year_match:
        year_default = int(year_match.group(1))

    out: dict[str, dict[str, Any]] = {}
    prev_end = 0
    for m in matches:
        # Walk the text between the end of the previous row and the start
        # of this one — that's where the course name lives. Header/noise
        # filtering is handled inside ``_extract_primary_name``: it walks
        # forward until it sees a degree-lead line (Bachelor, Master, …)
        # so the document title, column headers, and level dividers are
        # naturally skipped.
        name_region = text[prev_end : m.start()]
        prev_end = m.end()

        primary, extras = _extract_primary_name(name_region)
        if not primary:
            continue

        try:
            per_unit = int(m.group("per_unit").replace(",", ""))
            annual = int(m.group("annual").replace(",", ""))
            total = int(m.group("total").replace(",", ""))
        except (ValueError, AttributeError):
            continue

        # Sanity bounds match the uni-wide picker.
        if not (1000 < total < 200000):
            continue

        # ``Total Course Fee`` in ASA's table is exactly that — the full
        # programme cost. Mark accordingly so the dashboard label is
        # right and the per-course value isn't misread as a single-year
        # number.
        term = "Full Course" if total > annual else "Annual"

        # Key by CRICOS, NOT by normalized token set. CRICOS codes are
        # nationally unique per course, so a Certificate / Diploma /
        # Master sharing a stem ("Public Health") get their own
        # entries instead of collapsing onto one key and silently
        # dropping the lower-fee rows. (PR-7 review found that
        # token-set keying was Torrens-class data loss waiting to
        # happen.)
        cricos = m.group("cricos")
        out[cricos] = {
            "international_fee": total,
            "currency": "AUD",
            "fee_term": term,
            "fee_year": year_default,
            # Private fields used by the matcher only:
            # Match text combines primary + "Including Majors" extras so
            # variant names like "Bachelor of Business Hospitality
            # Management" can match the parent CRICOS row.
            "_pdf_match_text": f"{primary} {extras}".strip(),
            "_pdf_primary_name": primary,
            "_cricos": cricos,
        }

    return out


def match_course_in_pdf_table(
    course_name: str, by_course: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """Find the best PDF row for a given DB course name.

    Strategy: tokenise both names with :func:`_name_tokens` (stopwords
    removed) and require **at least two shared distinctive tokens** OR
    full token-set equality. Among qualifying rows pick the one with
    the highest ``max(db_coverage, pdf_coverage)`` score (≥ 0.5).

    The two-token floor is the fix for PR-7 review finding #2: with the
    old single-direction ``max`` score, a generic short PDF row like
    "Master of Design" → tokens ``{design}`` would score 1.0 against
    *any* DB course containing "design" (e.g. "Bachelor of Interior
    Design Residential"), causing cross-course fee contamination —
    exactly the failure mode this whole PR is meant to prevent. The
    exact-set escape hatch keeps short legitimate matches working
    (e.g. PDF "Master of Design" → DB "Master of Design").

    Returns ``None`` when no PDF row qualifies, so we fall back to the
    uni-wide value rather than mis-stamp a course.
    """
    db_tokens = _name_tokens(course_name)
    db_level = _degree_level(course_name)
    if not db_tokens or not by_course:
        return None

    best: tuple[tuple[float, int], dict[str, Any]] | None = None
    for row in by_course.values():
        primary = row.get("_pdf_primary_name") or ""
        pdf_primary_tokens = _name_tokens(primary)
        # Also count tokens from the "Including Majors" sub-list, so
        # "Bachelor of Business International Business" matches
        # "Bachelor of Business" even though "international" only
        # appears in the sub-list. The *primary*-only set is kept
        # separately for the exact-match escape hatch below.
        pdf_tokens = pdf_primary_tokens | _name_tokens(
            row.get("_pdf_match_text") or ""
        )
        if not pdf_tokens:
            continue

        # Hard filter on award level: when both sides expose a level
        # and they disagree, skip this row. Stops Cert/Diploma/Master
        # variants of the same stem (e.g. Public Health) from
        # cross-matching after stopword stripping erases their level.
        # When either side is unlabelled (rare), fall through to the
        # token-overlap scorer.
        pdf_level = _degree_level(primary)
        if db_level and pdf_level and db_level != pdf_level:
            continue

        overlap = len(db_tokens & pdf_tokens)
        if overlap == 0:
            continue

        # Escape hatch: parent rows like "Bachelor of Business" carry
        # only a single distinctive token (``business``), which would
        # otherwise be rejected by the ≥2 floor. Accept iff the DB
        # course's distinctive tokens are *exactly* those of the PDF
        # row's primary name (NOT the union with extras — those are
        # too permissive). This catches "Bachelor of Business" → its
        # own row, but rejects unrelated short cases like "Master of
        # Design" matching "Bachelor of Interior Design Residential"
        # (token sets {design} vs {interior, design, residential} are
        # not equal, so the escape hatch does NOT fire).
        exact_primary_match = (
            bool(pdf_primary_tokens) and db_tokens == pdf_primary_tokens
        )

        # Floor: need ≥2 distinctive tokens in common, unless the
        # exact-primary escape hatch fires.
        if overlap < 2 and not exact_primary_match:
            continue

        # ``max`` of the two coverages is the ranking signal — it
        # rewards the "PDF row name is a near-subset of a longer DB
        # name" case (e.g. PDF row "Master of Software Application
        # Design" matching DB "Master of IT (Software Application
        # Development)" → 2/3 = 0.67 pdf-coverage, more discriminating
        # than a coincidence on two generic tokens).
        score = max(overlap / len(db_tokens), overlap / len(pdf_tokens))
        if score < 0.5:
            continue
        # Tie-break preference: when two rows score equal, the one
        # whose primary-name token set is closer in size to the DB
        # course wins. Stops a row whose extras accidentally pad the
        # union from edging out a more specific row.
        size_delta = abs(len(pdf_primary_tokens) - len(db_tokens))
        key = (score, -size_delta)
        if best is None or key > best[0]:
            best = (key, row)

    if best is None:
        return None

    # Strip the private match-helper fields before returning to callers.
    return {k: v for k, v in best[1].items() if not k.startswith("_")}


async def _parse_fee_pdf(url: str, country: str | None, emit=None) -> dict[str, Any]:
    raw = await _download_raw_pdf(url)
    if not raw:
        return {}
    text = ""
    try:
        # ``download_pdf_text`` re-fetches the URL; instead reuse the
        # bytes we already have via the in-memory ``PdfReader`` path.
        from io import BytesIO

        from pypdf import PdfReader

        reader = PdfReader(BytesIO(raw))
        text = "\n".join((p.extract_text() or "") for p in reader.pages[:80])
    except Exception as exc:  # noqa: BLE001
        log.debug("fee PDF text extraction failed for %s: %s", url, exc)
        text = ""

    out: dict[str, Any] = {}
    by_course: dict[str, dict[str, Any]] = {}
    if text:
        # NEW: per-course table parser runs first. When the PDF is a
        # multi-row schedule (ASA, Torrens, …), this returns one row
        # per course so each course gets its OWN fee — no more "max
        # amount stamped on every sibling".
        by_course = _pick_per_course_amounts(text)

        # Bug G: try the PDF-specific pickAmounts heuristic. The
        # single-page fee extractor is preserved as a safety net for the
        # rare case where the PDF is actually a one-page web-style page
        # with a single tuition number. We still need this uni-wide
        # value as the fallback for courses that don't match a row.
        out = _pick_amounts_from_pdf_text(text)
        if not out:
            html = _wrap_text_as_html(text)
            try:
                results = await fee.extract(html, url, country=country)
                out = _first_filled(results, _FEE_KEYS)
            except Exception as exc:  # noqa: BLE001
                log.warning("fee extractor failed on PDF %s: %s", url, exc)

    # Vision fallback fires when the text path returned no usable fee data
    # — typically a scanned/image-only PDF.
    if not out and not by_course:
        vision_text = await _vision_fallback_text(raw, "fee", url, emit)
        if vision_text:
            by_course = _pick_per_course_amounts(vision_text)
            out = _pick_amounts_from_pdf_text(vision_text)
            if not out:
                html = _wrap_text_as_html(vision_text)
                try:
                    results = await fee.extract(html, url, country=country)
                    out = _first_filled(results, _FEE_KEYS)
                except Exception as exc:  # noqa: BLE001
                    log.warning("fee extractor failed on vision text for %s: %s", url, exc)
    if out:
        log.info("fee PDF %s yielded %s", url, sorted(out))
    if by_course:
        log.info(
            "fee PDF %s yielded %d per-course rows: %s",
            url,
            len(by_course),
            sorted({r.get("_pdf_primary_name", "?") for r in by_course.values()}),
        )
        # Stash the per-course map under a private key so the merge layer
        # can reach it without changing the existing top-level shape.
        out = dict(out)
        out["_by_course"] = by_course
    return out


async def _parse_requirements_pdf(url: str, emit=None) -> dict[str, Any]:
    raw = await _download_raw_pdf(url)
    if not raw:
        return {}
    text = ""
    try:
        from io import BytesIO

        from pypdf import PdfReader

        reader = PdfReader(BytesIO(raw))
        text = "\n".join((p.extract_text() or "") for p in reader.pages[:80])
    except Exception as exc:  # noqa: BLE001
        log.debug("requirements PDF text extraction failed for %s: %s", url, exc)
        text = ""

    out: dict[str, Any] = {}
    if text:
        html = _wrap_text_as_html(text)
        try:
            results = await english_test.extract(html, url)
            out = _first_filled(results, _ENGLISH_KEYS)
        except Exception as exc:  # noqa: BLE001
            log.warning("english extractor failed on PDF %s: %s", url, exc)

    if not out:
        vision_text = await _vision_fallback_text(raw, "requirements", url, emit)
        if vision_text:
            html = _wrap_text_as_html(vision_text)
            try:
                results = await english_test.extract(html, url)
                out = _first_filled(results, _ENGLISH_KEYS)
            except Exception as exc:  # noqa: BLE001
                log.warning("english extractor failed on vision text for %s: %s", url, exc)
    if out:
        log.info("requirements PDF %s yielded %s", url, sorted(out))
    return out


async def load_university_pdf_data(
    scrape_config: dict[str, Any] | None,
    country: str | None,
    *,
    emit=None,
) -> dict[str, Any]:
    """Read both PDFs (if configured) and return uni-level fallback data.

    Shape::

        {
            "fee": {"international_fee": 24000, "currency": "AUD", ...},
            "fee_by_course": {                                     # NEW
                "<normalized_pdf_name>": {
                    "international_fee": 52800, "currency": "AUD",
                    "fee_term": "Full Course", "fee_year": 2026,
                    "_pdf_primary_name": "Master of ...",
                    "_pdf_match_text": "...", "_cricos": "117606J",
                },
                ...
            },
            "english": {"ielts_overall": 6.0, "ielts_listening": 5.5, ...},
            "fees_pdf_url": "https://.../fees.pdf",      # only if data extracted
            "requirements_pdf_url": "https://.../req.pdf",
        }

    ``fee_by_course`` is present whenever the fee schedule PDF was a
    multi-row table (≥2 data rows parsed). It carries one entry per
    course in the schedule; downstream merge code looks each course up
    via :func:`match_course_in_pdf_table` and prefers the matched row
    over the uni-wide ``fee`` value.

    Empty dict if neither PDF is configured or both failed. Safe to call
    even when ``scrape_config`` is ``None``.

    ``emit`` is the same async log-callback used elsewhere in the
    pipeline; when provided, vision-OCR fallback emits ``[FALLBACK]``
    lines so reviewers can see when AI is reading a scanned PDF.
    """
    pages = ((scrape_config or {}).get("uniPages") or {})
    fees_pdf_url = (pages.get("feesPdf") or "").strip()
    reqs_pdf_url = (pages.get("requirementsPdf") or "").strip()

    fee_data = await _parse_fee_pdf(fees_pdf_url, country, emit=emit) if fees_pdf_url else {}
    english_data = await _parse_requirements_pdf(reqs_pdf_url, emit=emit) if reqs_pdf_url else {}

    out: dict[str, Any] = {}
    if fee_data:
        # Pop the per-course map off the inner dict so the public
        # ``fee`` block stays the same shape it has always been (just
        # the uni-wide values).
        by_course = fee_data.pop("_by_course", None)
        if fee_data:
            out["fee"] = fee_data
        if by_course:
            out["fee_by_course"] = by_course
        if fee_data or by_course:
            out["fees_pdf_url"] = fees_pdf_url
    if english_data:
        out["english"] = english_data
        out["requirements_pdf_url"] = reqs_pdf_url
    return out
