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


async def _save_pdf_snapshot_safe(url: str, raw: bytes) -> None:
    """Upload raw PDF bytes to S3 and write a PageSnapshot row.

    Awaited directly after the PDF download so the snapshot is committed
    before extraction continues.  All failures are logged as warnings and
    the scrape continues normally (non-fatal).

    Respects all safeguards:
      • is_enabled()     — SNAPSHOT_ENABLED=false → skip
      • is_replay_mode() — skip during replay so snapshots are not overwritten
      • get_snapshot_context() → missing uni_id/job_id → skip silently
    """
    try:
        from app.services.scraper.snapshot_context import (
            get_snapshot_context,
            is_replay_mode,
        )
        if is_replay_mode():
            return

        uni_id, job_id = get_snapshot_context()
        if not uni_id or not job_id:
            return

        from app.services.snapshot_store import (
            is_enabled,
            upload_snapshot,
            url_hash as _url_hash,
        )
        if not is_enabled():
            return

        key = await upload_snapshot(
            raw,
            university_id=uni_id,
            scrape_job_id=job_id,
            url=url,
            snapshot_type="pdf",
            content_type="application/pdf",
        )
        if not key:
            return

        from datetime import datetime, timezone

        from app.database import AsyncSessionLocal
        from app.models.page_snapshot import PageSnapshot

        async with AsyncSessionLocal() as db:
            snap = PageSnapshot(
                university_id=uni_id,
                scrape_job_id=job_id,
                course_url=url,
                url_hash=_url_hash(url),
                snapshot_type="pdf",
                storage_path=key,
                status_code=200,
                content_length=len(raw),
                fetch_method="pdf_pipeline",
                fetched_at=datetime.now(timezone.utc),
            )
            db.add(snap)
            await db.commit()

        log.debug(
            "pdf snapshot saved: uni=%s job=%s url=%s bytes=%d",
            uni_id, job_id, url, len(raw),
        )

    except Exception as exc:  # noqa: BLE001
        log.warning(
            "pdf snapshot save failed (non-fatal — scrape continues): %s: %s",
            type(exc).__name__, exc,
        )


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


def _normalize_spaced_pdf_text(text: str) -> str:
    """Collapse PDFs where each glyph is space-separated (e.g. KOI fee schedule).

    pypdf extracts KOI's fee PDF with every character space-separated::

        'D i p l o m a  o f  A c c o u n t i n g'  (double spaces between words)
        '0 7 0 3 6 8 K'                              (single spaces between digits)
        '$ 7 , 2 5 0'                                (single spaces everywhere)

    Strategy: heuristically detect a spaced-character PDF (>60 % of tokens are
    single characters), then for each line replace runs of 2+ spaces with a
    placeholder word-boundary, strip remaining single spaces (intra-character
    gaps), and restore the placeholder as a regular space.

    Leaves normal PDFs unchanged (fast-path: returns *text* as-is when the
    single-character fraction is below the detection threshold).
    """
    tokens = text.split()
    if not tokens:
        return text
    single_frac = sum(1 for t in tokens if len(t) == 1) / len(tokens)
    if single_frac < 0.60:
        return text  # Normal PDF — skip normalization

    lines = text.split("\n")
    result: list[str] = []
    for line in lines:
        if not line.strip():
            result.append("")
            continue
        # 1. Mark word boundaries (2+ consecutive spaces) with a placeholder.
        normalized = re.sub(r"  +", "\x00", line)
        # 2. Remove remaining intra-character single spaces.
        normalized = normalized.replace(" ", "")
        # 3. Restore word boundaries as single spaces.
        normalized = normalized.replace("\x00", " ").strip()
        result.append(normalized)

    return "\n".join(result)


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
    # CRICOS course codes are *normally* 6 digits + 1 trailing letter
    # (e.g. 102219K, 117606J). Some publishers print 7-digit codes
    # without a trailing letter (Torrens 2026 schedule has rows like
    # ``0101388 2 16 $5,156 …``), and pypdf occasionally concatenates
    # the trailing letter into the next number. We accept either shape
    # and let the rest of the row (duration + units + three $-amounts)
    # discriminate true data rows from coincidental digit runs.
    #
    # Anchoring with \b on both sides avoids matching unit counts
    # ("15*") or fee-amount digits ("19,360"). NOT line-anchored
    # because pypdf often concatenates a course name and its data row
    # onto a single line ("Diploma of Business  108861B 1 8 …"), so
    # requiring ^\s* would silently skip those rows.
    #
    # Duration accepts decimals (Torrens postgrad uses 0.5 / 1.5 / 1.7
    # year increments) and an optional ``Months`` qualifier — earlier
    # versions only matched integer years and silently dropped every
    # half-year row, which then got swallowed as continuation text
    # into the previous row's primary name and polluted the matcher.
    # Fee columns are OPTIONAL: many fee schedules print "TBA", "N/A",
    # "—", a dash, or simply leave the cell blank for courses whose fee
    # has not yet been published. Earlier versions required three literal
    # ``$xxx`` captures and silently dropped every such row, leading to
    # Torrens-class coverage loss (32 of 110 rows extracted). We now
    # accept either a ``$amount`` or a placeholder/blank, and downstream
    # logic treats missing amounts as ``None`` instead of failing the
    # whole row.
    r"\b(?P<cricos>\d{6}[A-Z]|\d{7,8})\b\s+"
    r"(?P<duration>\d+(?:\.\d+)?(?:\s*Months?)?)\s+"
    r"(?P<units>\d+\*?)\s+"
    r"(?:\$(?P<per_unit>[\d,]+)|(?P<per_unit_alt>TBA|N/?A|[—\-–]+))\s+"
    r"(?:\$(?P<annual>[\d,]+)|(?P<annual_alt>TBA|N/?A|[—\-–]+))\s+"
    r"(?:\$(?P<total>[\d,]+)|(?P<total_alt>TBA|N/?A|[—\-–]+))",
    re.I,
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


# Australian campus tokens that PDF schedules sometimes append to the
# primary course-name column (e.g. Torrens: "Bachelor of Business
# (Accounting) Sydney, Melbourne, Online"). We strip these from the
# tail of an extracted primary so token-based matching against the DB
# course name (which has no campus info) doesn't get drowned out.
_CAMPUS_TAIL_TOKENS = {
    "sydney", "melbourne", "adelaide", "brisbane", "perth",
    "darwin", "canberra", "hobart", "auckland", "wellington",
    "online", "australia", "campus", "domestic", "international",
    "fortitude", "valley",  # Brisbane suburb sometimes spelled out
}


def _strip_campus_tail(primary: str) -> str:
    """Remove a trailing comma-separated campus list from a primary name.

    Examples::

        "Bachelor of Business (Accounting) Sydney, Melbourne, Online"
            -> "Bachelor of Business (Accounting)"
        "Master of Public Health Sydney"
            -> "Master of Public Health"
        "Bachelor of Professional Accounting"
            -> "Bachelor of Professional Accounting"  # unchanged

    Stops as soon as a non-campus token is reached, so legitimate
    continuation words ("Accounting", "Design", etc.) at the tail are
    preserved. Also tolerates trailing commas left over from comma
    splitting.
    """
    if not primary:
        return primary
    parts = primary.split()
    while parts:
        # Allow either a bare campus token or a campus token followed
        # by a comma (e.g. "Sydney,").
        bare = parts[-1].rstrip(",").lower()
        if bare in _CAMPUS_TAIL_TOKENS:
            parts.pop()
        else:
            break
    return " ".join(parts).rstrip(",").strip()


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
    # Strip trailing internal academic-catalogue product codes from the
    # primary name (e.g. Federation: "Bachelor of Nursing NN5",
    # "Bachelor of Business (Accounting) BU5.ACC", "Bachelor of
    # Information Technology (Business Analysis) IT5.BA",
    # "Master of Engineering Technology (Mining) EZ9.MIN",
    # "Graduate Certificate in Maintenance Management** GMM4"). These
    # codes appear AFTER the human-readable course title and BEFORE the
    # CRICOS identifier in tabular fee schedules. Without this strip,
    # the parser folds the code into the primary name, which inflates
    # the PDF token bag with noise tokens (``nn5``, ``bu5``, ``it5``)
    # and prevents single-token DB course names ("Bachelor of Nursing"
    # → ``{nursing}``) from passing the exact-primary escape hatch in
    # ``match_course_in_pdf_table`` — Federation symptom: 18+ courses
    # with no fee even though the PDF row exists.
    #
    # Pattern: 2-4 uppercase letters + 1-3 digits + optional ".SUFFIX",
    # anchored to the end of the primary, with optional trailing "*"
    # footnote markers. Conservative: only fires when the rest of the
    # primary still has at least 2 words so legitimate short titles are
    # never mangled.
    _code_strip = re.sub(
        r"\s+\*{0,2}[A-Z]{2,4}\d{1,3}(?:\.[A-Z]+)?\*{0,2}\s*$", "", primary
    )
    if _code_strip != primary and len(_code_strip.split()) >= 2:
        primary = _code_strip
    # Strip trailing campus suffixes from the primary so token-based
    # matching against the DB course name (which never carries campus
    # info) doesn't get drowned out. The dropped tokens are folded
    # back into ``extras`` so they're still available to the matcher
    # as low-priority enrichment context (and so the data isn't lost
    # for downstream consumers that want it).
    cleaned = _strip_campus_tail(primary)
    if cleaned != primary:
        dropped = primary[len(cleaned):].strip()
        if dropped:
            extras = (extras + " " + dropped).strip() if extras else dropped
        primary = cleaned
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

    # Per-uni override: when YAML supplies extraction.fees.pdf_row_pattern,
    # use it instead of the shared _PDF_DATA_ROW_RE. Lets a uni whose fee
    # PDF uses a different column layout (e.g. Federation: CRICOS + 2
    # dollar amounts, no per-unit / total) opt-in to its own regex
    # without disturbing every other uni. None (default) → unchanged
    # behaviour.
    row_re = _PDF_DATA_ROW_RE
    pdf_fee_term_override: str | None = None
    prefer_annual: bool = False
    try:
        from app.services.scraper.config.context import get_uni_config

        _cfg = get_uni_config()
        if _cfg is not None:
            _pat = getattr(_cfg.extraction.fees, "pdf_row_pattern", None)
            if _pat:
                row_re = re.compile(_pat, re.I)
                log.debug("PDF row pattern override applied (per-uni YAML).")
            pdf_fee_term_override = getattr(
                _cfg.extraction.fees, "pdf_fee_term", None
            )
            prefer_annual = bool(
                getattr(_cfg.extraction.fees, "prefer_annual_over_total", False)
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Per-uni PDF row pattern override failed (%s); falling back "
            "to shared _PDF_DATA_ROW_RE.",
            exc,
        )
        row_re = _PDF_DATA_ROW_RE
        pdf_fee_term_override = None
        prefer_annual = False

    # Collect the matches AND track end-positions so we can slice out the
    # text BEFORE each row as the candidate course name.
    matches = list(row_re.finditer(text))
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

        # Each fee column is OPTIONAL — when the cell was TBA / N/A /
        # dash / blank the named groups capture None. We still want to
        # record the row (so the matcher can find the course by name and
        # avoid falsely falling back to the uni-wide value), but we mark
        # every fee field as None.
        def _parse_amount(g: str | None) -> int | None:
            if not g:
                return None
            try:
                return int(g.replace(",", ""))
            except ValueError:
                return None

        # Use groupdict().get() so per-uni override patterns that omit
        # one of these named groups (e.g. Federation only captures
        # CRICOS + a single annual amount) don't raise IndexError.
        gd = m.groupdict()
        per_unit = _parse_amount(gd.get("per_unit"))
        annual = _parse_amount(gd.get("annual"))
        total = _parse_amount(gd.get("total"))

        # Optional trailing duration (per-uni regex extension — Federation
        # 2026-05-10). When the override pattern captures `duration` +
        # `duration_unit` named groups (e.g. "1 year", "0.5 year",
        # "6 months", "2 semesters"), normalise to a (value, term) pair
        # so the downstream merge in single_course.py can fill payload
        # duration whenever the per-course HTML extractor came back NULL.
        # Empty for every uni whose pdf_row_pattern has no duration group.
        duration_pdf: float | None = None
        duration_term_pdf: str | None = None
        try:
            _dur_raw = gd.get("duration")
            _dur_unit = (gd.get("duration_unit") or "").strip().lower()
            if _dur_raw and _dur_unit:
                _dval = float(_dur_raw)
                if _dur_unit.startswith("year"):
                    duration_pdf, duration_term_pdf = _dval, "Year"
                elif _dur_unit.startswith("month"):
                    duration_pdf, duration_term_pdf = _dval, "Month"
                elif _dur_unit.startswith("semester"):
                    duration_pdf, duration_term_pdf = _dval, "Semester"
                elif _dur_unit.startswith("trimester"):
                    duration_pdf, duration_term_pdf = _dval, "Trimester"
        except (ValueError, TypeError):
            duration_pdf, duration_term_pdf = None, None

        # When the override pattern only carries an annual figure
        # (no per-unit / total — Federation), promote it to the
        # ``total`` slot so the rest of the pipeline (which keys
        # ``international_fee`` off ``total``) can use it.
        if total is None and annual is not None and per_unit is None:
            total = annual
            annual = None

        # Sanity bounds match the uni-wide picker, but only when a real
        # number was extracted — TBA/blank rows pass through with
        # ``total = None`` and no fee fields populated.
        if total is not None and not (1000 < total < 200000):
            continue

        # ``Total Course Fee`` in ASA's table is exactly that — the full
        # programme cost. Mark accordingly so the dashboard label is
        # right and the per-course value isn't misread as a single-year
        # number.
        if total is not None and annual is not None:
            term = "Full Course" if total > annual else "Annual"
        else:
            term = None

        # Per-uni YAML override: when the PDF only publishes annual
        # figures (no annual/total comparison possible) the auto-derived
        # term is None — fall back to the YAML-declared term so the
        # dashboard label is correct.
        if term is None and pdf_fee_term_override:
            term = pdf_fee_term_override

        # Per-uni YAML knob ``prefer_annual_over_total``: when both the
        # annual figure and the multi-year total are present (e.g.
        # Torrens 3-year Bachelor: $31,600 annual / $94,800 total),
        # store the annual figure with ``Annual`` term instead of the
        # default total / ``Full Course``. Off by default to preserve
        # historical behaviour for every other uni.
        emitted_fee = total
        if prefer_annual and annual is not None and total is not None:
            emitted_fee = annual
            term = "Annual"

        # Key by CRICOS, NOT by normalized token set. CRICOS codes are
        # nationally unique per course, so a Certificate / Diploma /
        # Master sharing a stem ("Public Health") get their own
        # entries instead of collapsing onto one key and silently
        # dropping the lower-fee rows. (PR-7 review found that
        # token-set keying was Torrens-class data loss waiting to
        # happen.)
        cricos = m.group("cricos")
        out[cricos] = {
            "international_fee": emitted_fee,
            "currency": "AUD" if emitted_fee is not None else None,
            "fee_term": term,
            "fee_year": year_default,
            # Optional duration lifted from the same PDF row (per-uni
            # regex must declare `duration` + `duration_unit` named
            # groups). None for every uni without those groups, so the
            # downstream merge is a no-op there.
            "duration_pdf": duration_pdf,
            "duration_term_pdf": duration_term_pdf,
            # Private fields used by the matcher only:
            # Match text combines primary + "Including Majors" extras so
            # variant names like "Bachelor of Business Hospitality
            # Management" can match the parent CRICOS row.
            "_pdf_match_text": f"{primary} {extras}".strip(),
            "_pdf_primary_name": primary,
            "_cricos": cricos,
        }

    return out


# ---------------------------------------------------------------------------
# Spaced-columnar PDF parser — KOI-style PDFs
# ---------------------------------------------------------------------------
# KOI publishes a fee schedule where pypdf yields each character as a separate
# token (space-separated) AND the table is laid out vertically (one field per
# line).  After _normalize_spaced_pdf_text() collapses the character spacing
# the page looks like:
#
#   Diploma of Accounting
#   070368K
#   52
#   $7,250
#   $14,500
#   Bachelor of Business (Accounting)
#   ...
#
# The standard _PDF_DATA_ROW_RE (horizontal row matcher) never fires on this
# layout; the uni-wide fallback then picks up a random large number as the
# fee and stamps it on every course.  This vertical parser fixes that.

_SPACED_COL_CRICOS_RE = re.compile(r"^\d{6}[A-Z]$|^\d{7,8}$")
_SPACED_COL_FEE_RE = re.compile(r"^\$([\d,]+(?:\.\d{1,2})?)$")
_SPACED_COL_DURATION_RE = re.compile(r"^\d{1,4}$")


def _pick_per_course_amounts_spaced_columnar(text: str) -> dict[str, dict[str, Any]]:
    """Parse KOI-style columnar fee PDFs after spaced-character normalization.

    After :func:`_normalize_spaced_pdf_text` each course block looks like::

        Diploma of Accounting   ← degree-lead line (possibly multi-line)
        070368K                 ← CRICOS code
        52                      ← duration in weeks
        $7,250                  ← per-trimester fee  (skipped)
        $14,500                 ← total course fee → international_fee

    Returns a CRICOS-keyed dict with the same shape as
    :func:`_pick_per_course_amounts` so downstream callers need no changes.
    Returns ``{}`` when fewer than 2 data rows are detected.
    """
    lines = [ln.strip() for ln in text.split("\n")]
    year_default: int | None = None
    yr = _FEE_YEAR_RE.search(text)
    if yr:
        year_default = int(yr.group(1))

    out: dict[str, dict[str, Any]] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line or not _PDF_DEGREE_LEAD_RE.match(line):
            i += 1
            continue

        # Accumulate multi-line course name until CRICOS or next degree-lead
        primary_parts: list[str] = [line]
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if not nxt:
                j += 1
                continue
            if _SPACED_COL_CRICOS_RE.match(nxt):
                break
            if _PDF_DEGREE_LEAD_RE.match(nxt):
                break
            # Accept short parenthetical / continuation text
            if nxt.startswith("(") or (
                len(nxt) < 80
                and not _SPACED_COL_FEE_RE.match(nxt)
                and not _SPACED_COL_DURATION_RE.match(nxt)
            ):
                primary_parts.append(nxt)
            j += 1

        raw_primary = " ".join(primary_parts).strip()
        primary, _extras = _extract_primary_name(raw_primary)
        if not primary:
            i += 1
            continue

        # CRICOS must be the very next non-empty line after the name block
        if j >= len(lines) or not _SPACED_COL_CRICOS_RE.match(lines[j]):
            i = j
            continue
        cricos = lines[j]
        j += 1

        # Optional duration (pure integer, e.g. "52" weeks)
        if j < len(lines) and _SPACED_COL_DURATION_RE.match(lines[j]):
            j += 1

        # Per-trimester fee (first $ amount after CRICOS / duration)
        if j >= len(lines) or not _SPACED_COL_FEE_RE.match(lines[j]):
            i = j
            continue
        j += 1  # skip per-trimester, we want the total

        # Total course fee (second $ amount)
        if j >= len(lines) or not _SPACED_COL_FEE_RE.match(lines[j]):
            i = j
            continue

        fee_m = _SPACED_COL_FEE_RE.match(lines[j])
        try:
            total = int(fee_m.group(1).replace(",", ""))  # type: ignore[union-attr]
        except (ValueError, AttributeError):
            i = j + 1
            continue

        if 1000 < total < 500_000:
            out[cricos] = {
                "international_fee": total,
                "currency": "AUD",
                "fee_term": "Full Course",
                "fee_year": year_default,
                "duration_pdf": None,
                "duration_term_pdf": None,
                "_pdf_match_text": primary,
                "_pdf_primary_name": primary,
                "_cricos": cricos,
            }
        i = j + 1

    if len(out) < 2:
        return {}
    return out


# Columnar PDF parser regexes — used by ``_pick_per_course_amounts_columnar``
# only, kept module-private so they don't leak into the legacy parser's
# behaviour. See _pick_per_course_amounts_columnar() for the full design.
_COL_CRICOS_RE = re.compile(r"\b(\d{6}[A-Z])\b")
_COL_FEE_RE = re.compile(r"\$([\d,]+)")
_COL_CITY_RE = re.compile(
    r"\b(Sydney|Melbourne|Brisbane|Adelaide|Perth|Online|Darwin|Canberra|"
    r"Hobart|Auckland|Wellington|Mountains|Townsville|Cairns|Newcastle|"
    r"Wollongong|Geelong|Ballarat|Bendigo|Gippsland|Berwick|Mt\.?\s*Helen)\b",
    re.I,
)
_COL_DEGREE_INNER_RE = re.compile(
    r"\b(Bachelor|Master|Graduate Certificate|Graduate Diploma|"
    r"Postgraduate Certificate|Postgraduate Diploma|Advanced Diploma|"
    r"Associate Degree|Associate Diploma|Diploma|Certificate|Doctor|"
    r"Doctorate|Honours|Honors)\b",
    re.I,
)
_COL_DEGREE_AT_START_RE = re.compile(
    r"^\s*(Bachelor|Master|Graduate Certificate|Graduate Diploma|"
    r"Postgraduate Certificate|Postgraduate Diploma|Advanced Diploma|"
    r"Associate Degree|Associate Diploma|Diploma|Certificate|Doctor|"
    r"Doctorate|Honours|Honors)\b",
    re.I,
)


def _pdftotext_layout(raw_pdf: bytes) -> str:
    """Run ``pdftotext -layout`` (poppler) over PDF bytes and return text.

    Returns ``""`` when the binary is missing or the subprocess fails so
    callers can transparently fall back to the pypdf path. ``-layout``
    preserves column alignment with whitespace, which is what
    :func:`_pick_per_course_amounts_columnar` needs to recover course
    names that wrap across multiple PDF table cells.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-", "-"],
            input=raw_pdf,
            capture_output=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning("pdftotext layout extraction failed (%s); falling back to pypdf", exc)
        return ""
    if result.returncode != 0:
        log.warning("pdftotext returned %s; stderr=%s", result.returncode, result.stderr[:200])
        return ""
    return result.stdout.decode("utf-8", errors="replace")


def _pick_per_course_amounts_columnar(text: str) -> dict[str, dict[str, Any]]:
    """Parse a per-course tuition table using CRICOS-anchored column slicing.

    Designed for ``pdftotext -layout`` output where the multi-column table
    structure is preserved via fixed-position whitespace. The legacy
    line-regex parser (:func:`_pick_per_course_amounts`) fails on PDFs
    whose course titles wrap across 2-3 lines (e.g. Torrens:
    ``Diploma of Branded`` / ``Fashion Design`` on separate lines) because
    pypdf flattens the layout and the row regex requires CRICOS+fees on a
    single line.

    Strategy:
      1. Each line containing ``\\b\\d{6}[A-Z]\\b`` (CRICOS) AND a ``$``
         amount is a row anchor.
      2. The course-name + campus blob lives at column ``[0, cricos_pos]``.
      3. Split off the campus column by locating the first city-name token
         in the blob (>= column 25 to skip Field-of-study prefix).
      4. Extract the course title proper by scanning the remaining text
         for the first degree-lead word.
      5. Walk forward up to 3 lines: if the line has no CRICOS / no fees /
         no degree-lead at start, treat its column-aligned slice as a
         wrap-line continuation of the current row's course name.
      6. Fees come from the trailing ``$`` amounts on the anchor line —
         ``total`` = last, ``annual`` = second-to-last.

    Returns ``{cricos: row_dict}`` keyed identically to the legacy parser
    so :func:`match_course_in_pdf_table` and the merge layer require no
    changes. Returns ``{}`` when fewer than 2 rows are extracted.
    """
    if not text:
        return {}
    lines = text.split("\n")
    cricos_lines = [
        (i, l) for i, l in enumerate(lines)
        if _COL_CRICOS_RE.search(l) and _COL_FEE_RE.search(l)
    ]
    if len(cricos_lines) < 2:
        return {}

    year_default = None
    year_match = _FEE_YEAR_RE.search(text)
    if year_match:
        year_default = int(year_match.group(1))

    # Per-uni knob (mirrors ``_pick_per_course_amounts``): prefer the
    # annual column over the total-course column when both are present.
    prefer_annual: bool = False
    try:
        from app.services.scraper.config.context import get_uni_config

        _cfg = get_uni_config()
        if _cfg is not None:
            prefer_annual = bool(
                getattr(_cfg.extraction.fees, "prefer_annual_over_total", False)
            )
    except Exception:  # noqa: BLE001
        prefer_annual = False

    rows: dict[str, dict[str, Any]] = {}
    for idx, anchor in cricos_lines:
        m = _COL_CRICOS_RE.search(anchor)
        if not m:
            continue
        cricos = m.group(1)
        if cricos in rows:
            # First-seen wins on duplicate CRICOS (rare, but defensive).
            continue
        cricos_pos = m.start()
        blob = anchor[:cricos_pos]
        # Locate the campus column: first city token at column >= 25.
        # The 25-char floor skips the "Field of study" leading column so
        # categories like "Sydney School of Business" — if any — won't be
        # mistaken for a campus list.
        campus_match = None
        for cm in _COL_CITY_RE.finditer(blob):
            if cm.start() >= 25:
                campus_match = cm
                break
        course_part = (
            blob[: campus_match.start()].rstrip(" ,")
            if campus_match
            else blob.rstrip()
        )
        # The course title starts at the first degree-lead word — anything
        # before is the Field-of-study column header.
        dm = _COL_DEGREE_INNER_RE.search(course_part)
        if not dm:
            continue
        course_main = course_part[dm.start():].strip()

        # Walk forward to fold wrap-fragments at the same column slice.
        # Stop at any of: next CRICOS row, line with fee data, line that
        # starts with a degree word (= next row's course title), or a
        # section header like "Undergraduate" / "Postgraduate".
        wrap_after: list[str] = []
        for off in (1, 2, 3):
            j = idx + off
            if j >= len(lines):
                break
            nl = lines[j]
            if _COL_CRICOS_RE.search(nl):
                break
            if _COL_FEE_RE.search(nl):
                break
            slice_text = nl[:cricos_pos].rstrip()
            # Drop the campus column from the wrap line too.
            cm2 = _COL_CITY_RE.search(slice_text)
            frag = slice_text[: cm2.start()] if cm2 else slice_text
            frag = frag.strip(" ,")
            if not frag:
                continue
            if _COL_DEGREE_AT_START_RE.match(frag):
                break
            if frag.lower() in ("undergraduate", "postgraduate"):
                break
            wrap_after.append(frag)

        primary = re.sub(r"\s+", " ", (course_main + " " + " ".join(wrap_after)).strip())

        # Extract fee amounts from the anchor line tail (after CRICOS).
        amounts = [
            int(a.replace(",", ""))
            for a in _COL_FEE_RE.findall(anchor[m.end():])
        ]
        if not amounts:
            continue
        total = amounts[-1]
        annual = amounts[-2] if len(amounts) >= 2 else None
        if not (1000 < total < 200000):
            continue
        if annual is not None and total > annual:
            term = "Full Course"
        elif annual is not None:
            term = "Annual"
        else:
            term = None

        # Per-uni knob: prefer annual when the row exposes the full
        # 3-column shape (per-unit + annual + total — e.g. Torrens
        # 3-year Bachelor: $4,450 / $31,600 / $94,800). Requires
        # ``len(amounts) >= 3`` because in this columnar parser the
        # second-to-last amount is positional, not semantic — for a
        # 2-amount row [per_unit, annual] we would otherwise emit
        # the per-unit figure under fee_term="Annual". Off by default;
        # preserves historical "report total" behaviour for every
        # other uni.
        emitted_fee = total
        if (
            prefer_annual
            and annual is not None
            and len(amounts) >= 3
            and total > annual
        ):
            emitted_fee = annual
            term = "Annual"

        rows[cricos] = {
            "international_fee": emitted_fee,
            "currency": "AUD",
            "fee_term": term,
            "fee_year": year_default,
            "_pdf_match_text": primary,
            "_pdf_primary_name": primary,
            "_cricos": cricos,
        }

    return rows


def match_course_in_pdf_table(
    course_name: str,
    by_course: dict[str, dict[str, Any]],
    cricos_code: str | None = None,
    course_pdf_aliases: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Find the best PDF row for a given DB course name.

    Returns a ``(row, method_suffix)`` tuple where ``method_suffix`` is
    ``"cricos_match"`` when a direct CRICOS key hit was used, or
    ``"name_match"`` when the fuzzy name-token path fired.
    Returns ``(None, "no_match")`` when no PDF row qualifies.

    **CRICOS-first lookup**: when *cricos_code* is supplied and matches a
    key in *by_course* (which is already keyed by CRICOS), that row is
    returned directly — zero fuzzy matching needed.  This eliminates the
    "IT-Cyber Security gets IT-AI fees" failure mode that arises from
    overlapping distinctive tokens.

    **Fuzzy fallback**: tokenise both names with :func:`_name_tokens`
    (stopwords removed) and require **at least two shared distinctive
    tokens** OR full token-set equality. Among qualifying rows pick the
    one with the highest ``max(db_coverage, pdf_coverage)`` score (≥ 0.5).

    The two-token floor is the fix for PR-7 review finding #2: with the
    old single-direction ``max`` score, a generic short PDF row like
    "Master of Design" → tokens ``{design}`` would score 1.0 against
    *any* DB course containing "design" (e.g. "Bachelor of Interior
    Design Residential"), causing cross-course fee contamination —
    exactly the failure mode this whole PR is meant to prevent. The
    exact-set escape hatch keeps short legitimate matches working
    (e.g. PDF "Master of Design" → DB "Master of Design").

    Returns ``(None, "no_match")`` when no PDF row qualifies, so callers
    fall back to the
    uni-wide value rather than mis-stamp a course.
    """
    # Per-uni alias hook: when an operator has mapped this DB course name
    # to its actual PDF row title (e.g. Torrens "Master of Design" → PDF
    # row "Master of Design (Non-Cognate)"), swap in the alias for token
    # extraction. The DB course name still appears in logs and remains
    # the matching identity downstream — only the token bag used to score
    # PDF rows is enriched. Empty / unset → preserves the original
    # behaviour for every other university.
    matching_name = course_name
    if course_pdf_aliases and course_name:
        alias = course_pdf_aliases.get(course_name.strip().lower())
        if alias:
            matching_name = alias
            log.debug(
                "PDF alias applied for %r → matching as %r",
                course_name,
                alias,
            )

    db_tokens = _name_tokens(matching_name)
    db_level = _degree_level(matching_name) or _degree_level(course_name)
    if not db_tokens or not by_course:
        return None, "no_match"

    # CRICOS-first: when the caller has a CRICOS code and it is present as a
    # key in ``by_course`` (which _pick_per_course_amounts already keys by
    # CRICOS), skip fuzzy matching entirely.  Direct-key lookup is O(1) and
    # 100% accurate — no token confusion between related programmes.
    if cricos_code:
        cricos_row = by_course.get(cricos_code)
        if cricos_row:
            log.debug(
                "CRICOS-first hit for %r → CRICOS=%s fee=$%s",
                course_name,
                cricos_code,
                cricos_row.get("international_fee"),
            )
            cleaned = {k: v for k, v in cricos_row.items() if not k.startswith("_")}
            return cleaned, "cricos_match"

    best: tuple[tuple[float, int], dict[str, Any]] | None = None
    for row in by_course.values():
        primary = row.get("_pdf_primary_name") or ""
        pdf_primary_tokens = _name_tokens(primary)
        # Also count tokens from the "Including Majors" sub-list, so
        # "Bachelor of Business International Business" matches
        # "Bachelor of Business" even though "international" only
        # appears in the sub-list. The *primary*-only set is kept
        # separately for the exact-match escape hatch below.
        #
        # Defense-in-depth: a real "Including Majors:" sub-list rarely
        # exceeds a dozen distinctive tokens. If the extras blob has
        # more than ~25 tokens it almost certainly means the parser
        # swallowed unrelated text (footer paragraphs, the next row's
        # data, etc.). Trusting it would inflate the union and let
        # short generic queries (e.g. "Higher Degrees By Research")
        # match unrelated rows on coincidental token overlap. In that
        # case fall back to primary-only matching.
        extras_tokens = _name_tokens(row.get("_pdf_match_text") or "")
        if len(extras_tokens) > 25:
            extras_tokens = set()
        pdf_tokens = pdf_primary_tokens | extras_tokens
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
        return None, "no_match"

    # Strip the private match-helper fields before returning to callers.
    return {k: v for k, v in best[1].items() if not k.startswith("_")}, "name_match"


async def _parse_fee_pdf(url: str, country: str | None, emit=None) -> dict[str, Any]:
    raw = await _download_raw_pdf(url)
    if not raw:
        return {}
    await _save_pdf_snapshot_safe(url, raw)
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

    # Per-uni parser-strategy override: when YAML supplies
    # extraction.fees.pdf_parser="columnar", re-extract text via
    # ``pdftotext -layout`` (poppler) and use the CRICOS-anchored,
    # column-position-aware row parser. This fixes PDFs whose course
    # titles wrap across 2-3 lines (Torrens fee schedule) — pypdf
    # flattens those into a single line and the legacy regex misses
    # them entirely. None / "legacy" / unset → unchanged behaviour for
    # every other university.
    pdf_parser_strategy: str | None = None
    try:
        from app.services.scraper.config.context import get_uni_config
        _cfg = get_uni_config()
        if _cfg is not None:
            pdf_parser_strategy = getattr(_cfg.extraction.fees, "pdf_parser", None)
    except Exception as exc:  # noqa: BLE001
        log.debug("pdf_parser strategy lookup failed (%s); using legacy", exc)
        pdf_parser_strategy = None

    out: dict[str, Any] = {}
    by_course: dict[str, dict[str, Any]] = {}
    if pdf_parser_strategy == "columnar":
        layout_text = _pdftotext_layout(raw)
        if layout_text:
            by_course = _pick_per_course_amounts_columnar(layout_text)
            log.info(
                "fee PDF %s: columnar parser produced %d rows",
                url,
                len(by_course),
            )
        if not by_course and text:
            # Safety net: columnar parser produced nothing (binary missing,
            # PDF unsupported, etc.) — fall back to legacy so the row was
            # never silently dropped relative to baseline.
            by_course = _pick_per_course_amounts(text)
            log.info(
                "fee PDF %s: columnar parser empty, legacy fallback produced %d rows",
                url,
                len(by_course),
            )
    elif pdf_parser_strategy == "spaced_columnar" and text:
        # KOI-style PDFs: every character is space-separated AND the table
        # is laid out vertically (one field per line). Normalize the spacing
        # first, then run the dedicated vertical parser.
        normalized_text = _normalize_spaced_pdf_text(text)
        by_course = _pick_per_course_amounts_spaced_columnar(normalized_text)
        log.info(
            "fee PDF %s: spaced_columnar parser produced %d rows",
            url,
            len(by_course),
        )
        if not by_course:
            # Safety net: fall back to legacy parser on the normalized text
            by_course = _pick_per_course_amounts(normalized_text)
            log.info(
                "fee PDF %s: spaced_columnar empty, legacy fallback on normalized text produced %d rows",
                url,
                len(by_course),
            )
        # Also run the uni-wide picker on the normalized text so the fallback
        # value is correct (rather than the AI-hallucinated 83500).
        if normalized_text:
            out = _pick_amounts_from_pdf_text(normalized_text)
    elif text:
        # NEW: per-course table parser runs first. When the PDF is a
        # multi-row schedule (ASA, Torrens, …), this returns one row
        # per course so each course gets its OWN fee — no more "max
        # amount stamped on every sibling".
        by_course = _pick_per_course_amounts(text)
    if text:

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
    await _save_pdf_snapshot_safe(url, raw)
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

    # Phase 6: extract academic entry requirements from the same PDF text
    # (ATAR, GPA, prior degree, work experience, portfolio/interview).
    # Rule-based — zero Gemini cost.  Stored as "_entry_req" so the caller
    # (load_university_pdf_data) can surface it separately from english data.
    _p6_text = text
    try:
        if not _p6_text and vision_text:  # vision_text defined inside 'if not out:' above
            _p6_text = vision_text
    except NameError:
        pass
    if _p6_text:
        try:
            from app.services.scraper.entry_req_extractor import extract_entry_requirements as _p6_ereq
            _p6_er = _p6_ereq(_p6_text)
            if _p6_er.confidence > 0.0:
                out["_entry_req"] = _p6_er.to_dict()
                log.info(
                    "[P6] entry_req extracted from %s: %s",
                    url.split("/")[-1][:40], _p6_er.to_summary_text()[:80],
                )
        except Exception as _p6_exc:  # noqa: BLE001
            log.debug("[P6] entry_req extraction failed for %s: %s", url, _p6_exc)
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
    # Phase 6: pop entry requirements dict before writing english_data to output
    _p6_entry_req: dict | None = english_data.pop("_entry_req", None)

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
    # Phase 6: attach entry requirements (ATAR, GPA, prior degree, …) from
    # the same PDF even when no english data was extracted (requirements PDF
    # may publish academic entry criteria without IELTS scores).
    if _p6_entry_req:
        out["entry_requirements"] = _p6_entry_req
        if not out.get("requirements_pdf_url") and reqs_pdf_url:
            out["requirements_pdf_url"] = reqs_pdf_url
    return out
