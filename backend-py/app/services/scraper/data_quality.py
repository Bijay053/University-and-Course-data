"""Scraper data-quality validation module.

Runs AFTER the staging loop and BEFORE the DONE event is emitted.  Inspects
every staged course payload to surface data-quality issues early — before they
reach operators or the publish queue.

Each issue is classified by severity:
    "critical"  — data is almost certainly wrong or missing; blocks publish.
    "warning"   — data may be wrong or incomplete; flags for review.
    "info"      — observation worth noting; does not block anything.

The module is intentionally read-only: it never mutates payloads or the DB.
It writes issue summaries to the live log via the ``emit`` callback and
returns a structured report that the orchestrator can include in the job record.

Usage (inside orchestrator.run_scrape_job, after staging loop):
    from app.services.scraper.data_quality import run_quality_checks
    quality_report = await run_quality_checks(staged_payloads, emit=emit)
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from app.services.scraper.config.schema import UniConfig

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Payload = dict[str, Any]
EmitFn = Callable[..., Awaitable[None]] | None

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


class QualityIssue:
    __slots__ = ("severity", "code", "message", "url", "course_name")

    def __init__(
        self,
        severity: str,
        code: str,
        message: str,
        url: str = "",
        course_name: str = "",
    ) -> None:
        self.severity = severity
        self.code = code
        self.message = message
        self.url = url
        self.course_name = course_name

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "url": self.url,
            "course_name": self.course_name,
        }


# ---------------------------------------------------------------------------
# Per-course checks
# ---------------------------------------------------------------------------

# Implausible fee boundaries (AUD) — absolute floor/ceiling used as a catch-all.
# Per-degree annual sanity ranges below are the primary quality signal.
_FEE_MIN = 500.0
_FEE_MAX = 250_000.0

# Per-degree-level annual fee sanity ranges (AUD/year).
# Tuple: (degree keywords, warn_min, crit_min, warn_max, crit_max)
# Applied when fee_term is annual/per-year (NOT a "Full Course" total).
_ANNUAL_FEE_RANGES: list[tuple[list[str], float, float, float, float]] = [
    (["doctorate", "phd", "ph.d"],         20_000, 12_000,  90_000, 130_000),
    (["master"],                            20_000, 12_000,  80_000, 120_000),
    (["graduate certificate"],              15_000,  8_000,  60_000,  90_000),
    (["graduate diploma"],                  15_000,  8_000,  65_000,  95_000),
    (["diploma"],                           10_000,  5_000,  60_000,  90_000),
    (["bachelor"],                          18_000, 10_000,  70_000, 100_000),
    (["associate"],                         10_000,  5_000,  60_000,  90_000),
    (["certificate"],                       10_000,  5_000,  55_000,  85_000),
]
_ANNUAL_FEE_RANGE_DEFAULT: tuple[float, float, float, float] = (15_000, 8_000, 80_000, 120_000)

# Approximate exchange rates to AUD for fee-range threshold normalisation.
# These are intentionally conservative (rounded) — the quality check only
# needs to avoid false positives on legitimate GBP/USD fees, not FX accuracy.
# Update when rates drift by >15%.  AUD is 1.0 (no conversion).
_CURRENCY_TO_AUD: dict[str, float] = {
    "AUD": 1.00,
    "GBP": 1.95,
    "USD": 1.55,
    "EUR": 1.65,
    "NZD": 0.90,
    "CAD": 1.12,
    "SGD": 1.15,
    "HKD": 0.20,
    "JPY": 0.011,
    "CNY": 0.22,
    "INR": 0.019,
    "MYR": 0.35,
}

def _annual_fee_range(dl: str) -> tuple[float, float, float, float]:
    for keywords, warn_min, crit_min, warn_max, crit_max in _ANNUAL_FEE_RANGES:
        if any(k in dl for k in keywords):
            return warn_min, crit_min, warn_max, crit_max
    return _ANNUAL_FEE_RANGE_DEFAULT


def _fee_to_aud(value: float, currency: str) -> float:
    """Convert a fee amount in *currency* to approximate AUD for threshold comparisons."""
    rate = _CURRENCY_TO_AUD.get(currency.upper(), 1.0)
    return value * rate

# Fee terms that mean annual/per-year (empty = assume annual).
_ANNUAL_FEE_TERMS: frozenset[str] = frozenset({
    "annual", "per year", "year", "yearly", "pa", "p.a.", "per annum",
    "semester", "per semester", "trimester", "per trimester",
    "per unit", "unit", "credit point", "eftsl",
})

# Fee values in this range for a supposedly international course almost always
# indicate a domestic Commonwealth Supported Place / HECS fee was captured.
_CSP_HECS_MAX = 13_000.0

# Implausible duration bounds
_DURATION_YEAR_MAX = 10.0
_DURATION_MONTH_MAX = 120.0
_DURATION_WEEK_MAX = 500.0

# Known generic-title fragments that indicate a category landing page slipped
# through discovery rather than a real individual course.
_GENERIC_TITLE_RE = re.compile(
    r"^\s*(?:bachelor(?:'?s)?\s+degrees?|master(?:'?s)?\s+degrees?|"
    r"postgraduate\s+(?:courses?|programs?|degrees?)|"
    r"undergraduate\s+(?:courses?|programs?)|"
    r"graduate\s+(?:certificate|diploma)\s*$|"
    r"diploma\s+programs?|certificate\s+programs?|"
    r"all\s+(?:courses?|programs?)|"
    r"(?:our\s+)?(?:courses?|programs?)\s*$)\s*$",
    re.IGNORECASE,
)

# Detects common footer / global campus fragments that indicate the location
# extractor grabbed site-wide content instead of course-specific data.
_JUNK_LOCATION_RE = re.compile(
    r"university\s+club|building\s+\d+|student\s+services|"
    r"administration|reception|library|sports\s+centre|"
    r"box\s+\d+|po\s+box|locked\s+bag|gpo\s+box",
    re.IGNORECASE,
)

# Navigation / header / menu text that is categorically not a campus name.
# Mirror of the same pattern in location.py — this catches anything that
# slipped through the extractor-level guard (e.g. via Gemini hallucination,
# sibling-cache, or PDF path) rather than the regex extractor.
_NAV_LOCATION_RE = re.compile(
    r"\b(?:scholarship|global\s+rankings?|accessibility\s+support|"
    r"admissions?\s+and\s+entry|apply\s+to\s+\w|entry\s+options?|"
    r"application\s+due\s+dates?|how\s+to\s+apply|"
    r"fees?\s+calculator|student\s+(?:life|hub|portal|login)|"
    r"international\s+students?\s+home|visit\s+(?:us|the\s+campus)|"
    r"career\s+(?:outcomes?|services?)|related\s+(?:courses?|programs?)|"
    r"contact\s+us|news\s+and\s+events|open\s+day|"
    r"research\s+(?:degrees?|programs?)|find\s+a\s+course)\b",
    re.IGNORECASE,
)

# Maximum plausible length for a campus/location string.
# Legitimate multi-campus strings like "Townsville, Cairns, Brisbane, Singapore"
# are ≤ 50 chars.  Anything over this threshold is almost certainly page prose
# or navigation text bled through the extractor.
_MAX_LOCATION_LEN = 80

# Domestic-fee context patterns — same family as _CSP_DOMESTIC_CTX in fee.py.
# Used to detect when a scrape has domestic fees only (CSP / HECS / govt-funded)
# with no international fee, which is a critical data gap for international portals.
_CSP_FEE_RE = re.compile(
    r"\b(?:commonwealth\s+supported\s+place|CSP|HECS(?:-HELP)?|"
    r"domestic\s+(?:student\s+)?(?:tuition\s+)?fee|government\s+supported)\b",
    re.IGNORECASE,
)

# Months we accept as valid intake months (title-cased).
_VALID_MONTHS = frozenset({
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
})


def _check_english_coherence(payload: Payload, url: str, name: str) -> list[QualityIssue]:
    """Flag English test values that contradict each other across fields.

    Each extractor (regex, vision, sibling_cache) writes fields independently.
    Without a cross-field check, a course can end up with IELTS 6.0 + TOEFL 95
    (two different admission levels) because each field was sourced from a
    different page or cache entry.

    Thresholds are deliberately permissive: we only flag combinations that are
    separated by ≥ 1 full IELTS band-width from any plausible equivalence.
    This means TOEFL 80 for an IELTS 5.5 course (above ETS official but used
    by many Australian universities) is NOT flagged, but TOEFL 95 for an
    IELTS 6.0 course (which corresponds to IELTS 7.0-7.5) IS flagged.

    All issues are "warning" severity — they flag for human review without
    blocking staging, because unusual-but-valid equivalences exist.
    """
    issues: list[QualityIssue] = []

    def _num(key: str) -> float | None:
        v = payload.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    ielts = _num("ielts_overall")
    if ielts is None:
        return issues  # no anchor — nothing to cross-check against

    toefl = _num("toefl_overall")
    pte = _num("pte_overall")
    duolingo = _num("duolingo_overall")
    cambridge = _num("cambridge_overall")

    def _add(code: str, msg: str) -> None:
        issues.append(QualityIssue("warning", code, msg, url=url, course_name=name))

    # ── IELTS vs TOEFL ──────────────────────────────────────────────────
    # TOEFL 85+ ≈ IELTS 6.5+; IELTS ≤ 6.0 + TOEFL ≥ 85 is a mismatch.
    # TOEFL 75- ≈ IELTS ≤ 5.5; IELTS ≥ 7.0 + TOEFL ≤ 75 is a mismatch.
    if toefl is not None:
        if ielts <= 6.0 and toefl >= 85:
            _add(
                "english_coherence_toefl",
                f"IELTS {ielts} + TOEFL {toefl} is inconsistent: "
                f"TOEFL {toefl:.0f} corresponds to IELTS ≥ 6.5. "
                f"One value is likely sourced from a different level or hallucinated.",
            )
        elif ielts >= 7.0 and toefl <= 75:
            _add(
                "english_coherence_toefl",
                f"IELTS {ielts} + TOEFL {toefl} is inconsistent: "
                f"TOEFL {toefl:.0f} corresponds to IELTS ≤ 5.5. "
                f"One value is likely sourced from a different level or hallucinated.",
            )

    # ── IELTS vs PTE ────────────────────────────────────────────────────
    # PTE 65+ ≈ IELTS 7.0+; IELTS ≤ 6.0 + PTE ≥ 65 is a mismatch.
    # PTE 45- ≈ IELTS ≤ 5.5; IELTS ≥ 7.0 + PTE ≤ 45 is a mismatch.
    if pte is not None:
        if ielts <= 6.0 and pte >= 65:
            _add(
                "english_coherence_pte",
                f"IELTS {ielts} + PTE {pte} is inconsistent: "
                f"PTE {pte:.0f} corresponds to IELTS ≥ 7.0. "
                f"One value is likely sourced from a different level or hallucinated.",
            )
        elif ielts >= 7.0 and pte <= 45:
            _add(
                "english_coherence_pte",
                f"IELTS {ielts} + PTE {pte} is inconsistent: "
                f"PTE {pte:.0f} corresponds to IELTS ≤ 5.0. "
                f"One value is likely sourced from a different level or hallucinated.",
            )

    # ── IELTS vs Duolingo ───────────────────────────────────────────────
    # Duolingo 115+ ≈ IELTS 7.0+; IELTS ≤ 6.0 + DET ≥ 115 is a mismatch.
    # Duolingo 95-  ≈ IELTS ≤ 5.5; IELTS ≥ 7.5 + DET ≤ 95 is a mismatch.
    if duolingo is not None:
        if ielts <= 6.0 and duolingo >= 115:
            _add(
                "english_coherence_duolingo",
                f"IELTS {ielts} + Duolingo {duolingo} is inconsistent: "
                f"Duolingo {duolingo:.0f} corresponds to IELTS ≥ 7.0. "
                f"Duolingo value may be hallucinated or from wrong level cache.",
            )
        elif ielts >= 7.5 and duolingo <= 95:
            _add(
                "english_coherence_duolingo",
                f"IELTS {ielts} + Duolingo {duolingo} is inconsistent: "
                f"Duolingo {duolingo:.0f} corresponds to IELTS ≤ 5.5. "
                f"Duolingo value may be hallucinated or from wrong level cache.",
            )

    # ── IELTS vs Cambridge (CAE) ─────────────────────────────────────────
    # Cambridge 176+ ≈ IELTS 7.0+; IELTS ≤ 6.0 + CAE ≥ 176 is a mismatch.
    # Cambridge 162- ≈ IELTS ≤ 5.5; IELTS ≥ 7.0 + CAE ≤ 162 is a mismatch.
    # Note: VIT shows CAE 176 on vocational courses with IELTS 5.5 — this fires
    # on those rows intentionally, since 176 is the C1 Advanced threshold (IELTS 7.0).
    if cambridge is not None:
        if ielts <= 6.0 and cambridge >= 176:
            _add(
                "english_coherence_cambridge",
                f"IELTS {ielts} + Cambridge {cambridge} is inconsistent: "
                f"CAE {cambridge:.0f} corresponds to IELTS ≥ 7.0 (C1 Advanced threshold). "
                f"Cambridge value may be a university-wide default that doesn't apply to this level.",
            )
        elif ielts >= 7.0 and cambridge <= 162:
            _add(
                "english_coherence_cambridge",
                f"IELTS {ielts} + Cambridge {cambridge} is inconsistent: "
                f"CAE {cambridge:.0f} corresponds to IELTS ≤ 5.5. "
                f"One value is likely sourced from a different level.",
            )

    return issues


def _check_course(
    payload: Payload,
    url: str,
    campus_allowlist: list[str] | None = None,
    default_currency: str = "AUD",
    require_international_fee: bool = True,
) -> list[QualityIssue]:
    """Return a list of quality issues for one staged course payload.

    Parameters
    ----------
    payload:
        Extracted course data dict.
    url:
        Source URL (used in issue records for traceability).
    campus_allowlist:
        When non-empty, the course_location must contain at least one of these
        strings (case-insensitive).  Sourced from ExtractionConfig.campus_allowlist
        in the per-uni YAML.  An empty list disables the check.
    require_international_fee:
        When False (sourced from staging.require_international_fee in the uni's
        YAML), the ``missing_international_fee`` issue is downgraded from CRITICAL
        to WARNING.  This prevents courses from landing in ``data_quality_failure``
        purely because the fee is behind a JS tab or a Cloudflare-protected page —
        the operator has already acknowledged the gap by setting the flag.
    """
    issues: list[QualityIssue] = []
    name = payload.get("course_name") or payload.get("name") or "?"

    def add(severity: str, code: str, msg: str) -> None:
        issues.append(QualityIssue(severity, code, msg, url=url, course_name=name))

    # ── 1. Course title ───────────────────────────────────────────────────
    if not name or name == "?":
        add("critical", "missing_course_name", "Course name is blank.")
    elif _GENERIC_TITLE_RE.match(name):
        add("critical", "generic_course_title",
            f"Title looks like a category page, not a specific course: {name!r}")
    elif len(name) < 8:
        add("warning", "suspiciously_short_title",
            f"Course title is very short ({len(name)} chars): {name!r}")
    if name and name != "?":
        # Validate that the universal course-name cleanup layer successfully
        # removed any university-name suffix.  If calling the cleaner here
        # still strips something it means the suffix survived extraction and
        # staging — the YAML aliases are likely incomplete.
        try:
            from app.services.scraper.course_name_cleaner import clean_course_name_with_config
            _, _leftover = clean_course_name_with_config(name)
            if _leftover:
                add(
                    "critical",
                    "university_name_in_course_title",
                    f"Course name still contains university suffix after cleanup: {name!r}. "
                    f"Suffix detected: {_leftover.strip()!r}. "
                    f"Add the alias to extraction.course_name.university_aliases in the uni's YAML.",
                )
        except Exception:
            pass

    # ── 2. Fee ───────────────────────────────────────────────────────────
    intl_fee = payload.get("international_fee")
    domestic_fee = payload.get("domestic_fee")
    has_central_fee = payload.get("has_central_fee_page")
    fee_term = (payload.get("fee_term") or "").strip()
    from app.services.scraper.currency_utils import default_currency as _dft_cur
    fee_currency = (
        payload.get("fee_currency") or default_currency or _dft_cur()
    ).strip().upper()
    duration_raw = payload.get("duration")
    duration_term_raw = (payload.get("duration_term") or "year").lower()
    degree_level_raw = (payload.get("degree_level") or "").lower()

    # Annual-equivalent thresholds by degree level (AUD/year).
    # Used when fee_term == "Full Course" to catch unreasonably high totals.
    _ANNUAL_THRESH: list[tuple[list[str], float]] = [
        (["doctorate", "phd", "ph.d"],          90_000.0),
        (["master"],                             70_000.0),
        (["graduate certificate"],               40_000.0),
        (["graduate diploma"],                   55_000.0),
        (["diploma"],                            50_000.0),
        (["bachelor"],                           60_000.0),
        (["associate"],                          45_000.0),
        (["certificate"],                        35_000.0),
    ]

    def _annual_threshold(dl: str) -> float:
        for keywords, thresh in _ANNUAL_THRESH:
            if any(k in dl for k in keywords):
                return thresh
        return 80_000.0  # conservative fallback

    if intl_fee is None:
        # Detect CSP / domestic-only fee situation — when a domestic fee
        # (HECS / Commonwealth Supported Place) is present but no international
        # fee was extracted, it means the page only published domestic pricing.
        # Flag as critical: international agents cannot use a CSP fee.
        if domestic_fee is not None:
            try:
                dom_val = float(domestic_fee)
                if 0 < dom_val < 30_000:
                    add(
                        "critical",
                        "domestic_fee_only_no_international",
                        f"Only a domestic fee found (${dom_val:,.0f}) with no international fee. "
                        f"This is likely a Commonwealth Supported Place / CSP / HECS fee. "
                        f"The page may not publish international pricing — leave blank rather "
                        f"than showing a misleading domestic figure.",
                    )
                    # Don't also fire missing_international_fee — that's noise on top
                    # of the more specific diagnostic above.
            except (TypeError, ValueError):
                pass
        if intl_fee is None and domestic_fee is None:
            if not has_central_fee:
                # When the university YAML sets require_international_fee=false,
                # the operator has acknowledged that the fee may be behind a JS
                # tab or a Cloudflare-protected endpoint.  Downgrade from CRITICAL
                # to WARNING so the course lands in the review queue (not
                # data_quality_failure) and can still be approved by a human.
                if require_international_fee:
                    add("critical", "missing_international_fee",
                        "No international fee found and no central fee page flag set.")
                else:
                    add("warning", "missing_international_fee",
                        "No international fee found (require_international_fee=false — "
                        "fee may be behind a JS tab or Cloudflare-protected page; "
                        "stage for human review).")
            else:
                add("warning", "missing_international_fee_central_page",
                    "International fee absent — marked for central fee page review.")
    else:
        try:
            fee_val = float(intl_fee)
            if fee_val < _FEE_MIN:
                add("critical", "fee_too_low",
                    f"International fee {fee_val:.0f} AUD is implausibly low "
                    f"(min threshold: {_FEE_MIN:.0f}).")
            elif fee_val > _FEE_MAX:
                add("critical", "fee_too_high",
                    f"International fee {fee_val:.0f} AUD is implausibly high "
                    f"(max threshold: {_FEE_MAX:.0f}).")

            # ── Full Course fee normalisation ──────────────────────────
            is_full_course = fee_term.lower() in ("full course", "full", "total", "full program")
            if is_full_course:
                # Always emit an info chip so operators know this is a total fee.
                add(
                    "info",
                    "full_course_fee_detected",
                    f"Fee {fee_val:,.0f} is marked as a full-course total (fee_term={fee_term!r}). "
                    "Annual equivalent should be verified against course duration.",
                )

                # Convert duration to years for the annual equivalent.
                dur_years: float | None = None
                if duration_raw is not None:
                    try:
                        d = float(duration_raw)
                        t = duration_term_raw
                        if "month" in t:
                            dur_years = d / 12.0
                        elif "week" in t:
                            dur_years = d / 52.0
                        elif "semester" in t or "trimester" in t:
                            dur_years = d / 2.0
                        else:
                            dur_years = d  # assume years
                    except (TypeError, ValueError):
                        pass

                if dur_years is None or dur_years <= 0:
                    add(
                        "critical",
                        "full_course_fee_no_duration",
                        f"Fee is marked as a full-course total ({fee_val:,.0f}) but no valid "
                        "duration was found. Cannot calculate the annual equivalent — "
                        "manual review required before publishing.",
                    )
                else:
                    annual_equiv = fee_val / dur_years
                    thresh = _annual_threshold(degree_level_raw)
                    if annual_equiv > thresh:
                        add(
                            "warning",
                            "full_course_fee_suspicious",
                            f"Full-course fee {fee_val:,.0f} ÷ {dur_years:.1f} yr = "
                            f"{annual_equiv:,.0f}/yr — exceeds the expected annual "
                            f"threshold of {thresh:,.0f}/yr for this degree level "
                            f"({degree_level_raw or 'unknown'}). "
                            "The fee may have been captured as an annual amount that "
                            "was mistakenly tagged as Full Course, or the total is genuinely "
                            "high. Review before publishing.",
                        )
                    else:
                        # Within threshold — emit info with the calculated equivalent.
                        add(
                            "info",
                            "full_course_fee_annual_ok",
                            f"Annual equivalent: {annual_equiv:,.0f}/yr "
                            f"({fee_val:,.0f} ÷ {dur_years:.1f} yr). "
                            "Within expected range — verify against source page.",
                        )

            # ── Annual fee sanity range (non-full-course terms) ────────────
            # When the fee is NOT tagged as a full-course total, validate it
            # against per-degree-level annual ranges.  This catches domestic/CSP
            # fees, partial fees, and total course fees labelled as annual.
            if not is_full_course:
                _fee_term_lc = fee_term.lower()
                # Treat blank fee_term and explicitly annual terms the same way.
                _is_annual_ctx = (
                    not _fee_term_lc or _fee_term_lc in _ANNUAL_FEE_TERMS
                )
                if _is_annual_ctx and _FEE_MIN <= fee_val <= _FEE_MAX:
                    _warn_min, _crit_min, _warn_max, _crit_max = _annual_fee_range(
                        degree_level_raw
                    )
                    # Normalise fee to AUD so GBP/USD/EUR fees are not falsely
                    # flagged against AUD thresholds.  fee_val remains in
                    # original currency for display; _fee_aud is used for logic.
                    _fee_aud = _fee_to_aud(fee_val, fee_currency)
                    _cur_label = fee_currency if fee_currency != "AUD" else "AUD"
                    _range_str = f"{_warn_min:,.0f}–{_warn_max:,.0f}/yr (AUD)"
                    _dl_label = degree_level_raw or "this degree level"
                    if _fee_aud < _crit_min:
                        if _fee_aud <= _CSP_HECS_MAX:
                            add(
                                "critical",
                                "possible_domestic_fee",
                                f"Fee {fee_val:,.0f} {_cur_label}/year "
                                f"(≈ {_fee_aud:,.0f} AUD) is in the domestic/CSP fee range "
                                f"(≤ {_CSP_HECS_MAX:,.0f} AUD). "
                                "This is likely a Commonwealth Supported Place, HECS, or domestic "
                                f"tuition fee — not an international annual fee. "
                                f"Expected international range for {_dl_label}: {_range_str}.",
                            )
                        else:
                            add(
                                "critical",
                                "annual_fee_too_low_critical",
                                f"Fee {fee_val:,.0f} {_cur_label}/year "
                                f"(≈ {_fee_aud:,.0f} AUD) is critically below the expected "
                                f"minimum for {_dl_label} (critical threshold: {_crit_min:,.0f} AUD/yr). "
                                "Likely a domestic, partial, or incorrectly extracted international fee. "
                                f"Expected range: {_range_str}.",
                            )
                    elif _fee_aud < _warn_min:
                        add(
                            "warning",
                            "annual_fee_too_low_warning",
                            f"Fee {fee_val:,.0f} {_cur_label}/year "
                            f"(≈ {_fee_aud:,.0f} AUD) is below the expected minimum for "
                            f"{_dl_label} (warning threshold: {_warn_min:,.0f} AUD/yr). "
                            "May be a partial or incorrectly extracted fee. "
                            f"Expected range: {_range_str}.",
                        )
                    elif _fee_aud > _crit_max:
                        add(
                            "critical",
                            "annual_fee_too_high_critical",
                            f"Fee {fee_val:,.0f} {_cur_label}/year "
                            f"(≈ {_fee_aud:,.0f} AUD) exceeds the critical upper limit for "
                            f"{_dl_label} (threshold: {_crit_max:,.0f} AUD/yr). "
                            "This value is likely a full-course total stored as an annual fee, "
                            f"not the actual yearly charge. Expected annual range: {_range_str}.",
                        )
                    elif _fee_aud > _warn_max:
                        add(
                            "warning",
                            "annual_fee_too_high_warning",
                            f"Fee {fee_val:,.0f} {_cur_label}/year "
                            f"(≈ {_fee_aud:,.0f} AUD) is above the expected maximum for "
                            f"{_dl_label} (warning threshold: {_warn_max:,.0f} AUD/yr). "
                            "Verify this is an annual fee and not a full-course total. "
                            f"Expected range: {_range_str}.",
                        )

        except (TypeError, ValueError):
            add("warning", "non_numeric_fee",
                f"International fee value is not numeric: {intl_fee!r}")

    # ── 3. IELTS / English requirement ───────────────────────────────────
    has_any_english = any(
        payload.get(k) is not None
        for k in (
            "ielts_overall", "ielts_reading", "ielts_writing",
            "ielts_listening", "ielts_speaking",
            "pte_overall", "toefl_overall", "cambridge_overall",
        )
    )
    if not has_any_english:
        # Postgraduate courses without any English requirement are a definite
        # data-quality failure: every AU/UK/NZ postgrad programme requires
        # international students to supply IELTS / PTE / TOEFL / CAE scores.
        # Leaving it as a mere warning means these courses pass the auto-publish
        # gate and appear live with no English entry requirement — misleading for
        # agents and students.  Escalate to critical so the course lands in
        # data_quality_failure and requires human review.
        _dl = (payload.get("degree_level") or "").lower()
        _is_postgrad = any(
            kw in _dl
            for kw in (
                "master", "graduate diploma", "graduate certificate",
                "doctorate", "ph", "pgce", "pgdip", "pgcert",
                "postgraduate",
            )
        )
        if _is_postgrad:
            add(
                "critical",
                "missing_english_requirement",
                "Postgraduate course has no English language test score "
                "(IELTS / PTE / TOEFL / CAE). International students cannot "
                "be assessed for admission — manual review required.",
            )
        else:
            add("warning", "missing_english_requirement",
                "No English language test score found (IELTS / PTE / TOEFL / CAE).")

    # ── 3a. Test-accepted flag violations ────────────────────────────────
    # If a university explicitly does not accept a test (*_accepted=False)
    # but the pipeline wrote a score for it anyway (from vision, sibling cache,
    # etc.), that is a definite pipeline error — the score cannot be correct.
    for _test_prefix, _flag_key, _score_key in (
        ("Duolingo",  "duolingo_accepted",  "duolingo_overall"),
        ("Cambridge", "cambridge_accepted", "cambridge_overall"),
        ("PTE",       "pte_accepted",       "pte_overall"),
        ("TOEFL",     "toefl_accepted",     "toefl_overall"),
    ):
        if payload.get(_flag_key) is False and payload.get(_score_key) is not None:
            add(
                "warning",
                f"test_not_accepted_but_scored_{_test_prefix.lower()}",
                f"{_test_prefix} score ({payload[_score_key]}) found but "
                f"{_flag_key}=False — university does not accept this test. "
                f"Score is likely hallucinated (vision or sibling cache). Discard it.",
            )

    # ── 3b. Cross-field English coherence ────────────────────────────────
    # Each test score should be broadly consistent with the others — if
    # the extractor wrote each field from a different source (regex, vision,
    # sibling cache) without cross-checking, impossible combinations can
    # appear (e.g. IELTS 6.0 + TOEFL 95 = two different admission levels).
    #
    # Thresholds are permissive (not the strict ETS official equivalence)
    # because Australian universities sometimes set their own stricter tables
    # (e.g. TOEFL 80 for IELTS 5.5 courses, which is above the ETS equivalent
    # of 72 but still within a defensible margin). We only flag combinations
    # that are clearly impossible — separated by ≥ 1 IELTS band-width.
    issues.extend(_check_english_coherence(payload, url, name))

    # ── 4. Duration ───────────────────────────────────────────────────────
    duration = payload.get("duration")
    duration_term = (payload.get("duration_term") or "").lower()
    if duration is None:
        add("warning", "missing_duration", "Duration not extracted.")
    else:
        try:
            dur_val = float(duration)
            if duration_term in ("year", "years"):
                if dur_val <= 0 or dur_val > _DURATION_YEAR_MAX:
                    add("warning", "suspicious_duration",
                        f"Duration {dur_val} year(s) is outside the expected range "
                        f"(0 < duration ≤ {_DURATION_YEAR_MAX}).")
            elif duration_term in ("month", "months"):
                if dur_val <= 0 or dur_val > _DURATION_MONTH_MAX:
                    add("warning", "suspicious_duration",
                        f"Duration {dur_val} month(s) is outside the expected range "
                        f"(0 < duration ≤ {_DURATION_MONTH_MAX}).")
            elif duration_term in ("week", "weeks"):
                if dur_val <= 0 or dur_val > _DURATION_WEEK_MAX:
                    add("warning", "suspicious_duration",
                        f"Duration {dur_val} week(s) is outside the expected range "
                        f"(0 < duration ≤ {_DURATION_WEEK_MAX}).")
        except (TypeError, ValueError):
            add("warning", "non_numeric_duration",
                f"Duration value is not numeric: {duration!r}")

    # ── 5. Intake months ─────────────────────────────────────────────────
    intake_months = payload.get("intake_months")
    if not intake_months:
        add("info", "missing_intake_months",
            "No intake months extracted from page.")
    elif isinstance(intake_months, list):
        invalid = [m for m in intake_months if m not in _VALID_MONTHS]
        if invalid:
            add("warning", "invalid_intake_months",
                f"Unrecognised intake month value(s): {invalid}")
        if len(intake_months) > 12:
            add("warning", "too_many_intake_months",
                f"intake_months has {len(intake_months)} entries — likely extraction noise.")

    # ── 6. Location ───────────────────────────────────────────────────────
    location = payload.get("course_location") or ""
    if not location.strip():
        add("info", "missing_location",
            "No course location extracted.")
    else:
        # Check for navigation / menu text that bled through the extractor.
        # This is a CRITICAL issue — the value is definitively wrong and must
        # never be published (e.g. "Global rankings Scholarships Accessibility
        # support Admissions and entry Apply to JCU Entry options …").
        if _NAV_LOCATION_RE.search(location):
            add(
                "critical",
                "nav_text_location",
                f"Location contains navigation/menu keywords — this is page header text, "
                f"not a campus name: {location[:120]!r}",
            )
        elif len(location) > _MAX_LOCATION_LEN and location.count(",") < 3:
            # Long string with few comma-separated segments is likely prose or
            # a nav block, not a list of campuses.
            add(
                "critical",
                "location_too_long",
                f"Location is {len(location)} chars with {location.count(',') + 1} segment(s) — "
                f"almost certainly page text rather than campus names: {location[:80]!r}…",
            )
        elif _JUNK_LOCATION_RE.search(location):
            add("warning", "suspicious_location",
                f"Location looks like footer/admin text rather than a campus name: {location!r}")

        # Campus allowlist check (per-uni YAML configurable).
        # Only fires when: (a) allowlist is non-empty, (b) location is set and
        # passed the nav-text checks above, (c) no allowlist entry appears in
        # the location string (case-insensitive).
        if campus_allowlist and not _NAV_LOCATION_RE.search(location):
            loc_lower = location.lower()
            if not any(campus.lower() in loc_lower for campus in campus_allowlist):
                add(
                    "critical",
                    "campus_not_in_allowlist",
                    f"Location {location!r} does not match any known campus: "
                    f"{campus_allowlist}. Verify the location extractor is reading "
                    f"the correct page element.",
                )

    # ── 7. Study mode ────────────────────────────────────────────────────
    study_mode = payload.get("study_mode") or ""
    if not study_mode.strip():
        add("info", "missing_study_mode",
            "Study mode not extracted — will show blank in Review UI.")

    # ── 8. Degree level ───────────────────────────────────────────────────
    if not payload.get("degree_level"):
        add("warning", "missing_degree_level", "Degree level not extracted.")

    return issues


# ---------------------------------------------------------------------------
# Duplicate detection across the batch
# ---------------------------------------------------------------------------

def _check_duplicates(payloads_with_urls: list[tuple[Payload, str]]) -> list[QualityIssue]:
    """Flag courses with identical names within the same scrape batch."""
    issues: list[QualityIssue] = []
    name_to_urls: dict[str, list[str]] = defaultdict(list)
    for payload, url in payloads_with_urls:
        name = (payload.get("course_name") or payload.get("name") or "").strip().lower()
        if name:
            name_to_urls[name].append(url)
    for name, urls in name_to_urls.items():
        if len(urls) > 1:
            issues.append(
                QualityIssue(
                    severity="warning",
                    code="duplicate_course_name",
                    message=(
                        f"Course name {name!r} appears {len(urls)} times in this batch. "
                        f"URLs: {', '.join(urls[:3])}"
                        + (" (…)" if len(urls) > 3 else "")
                    ),
                )
            )
    return issues


def _check_duplicate_fees(
    payloads_with_urls: list[tuple[Payload, str]],
    *,
    uni_config: Any | None = None,
) -> list[QualityIssue]:
    """Detect repeated fee values across courses — a strong indicator of a
    selector-scope reuse bug (the same DOM element being scraped for every
    course page). Fires when:
      • At least 5 courses have a fee, AND
      • ≥ 75% of fee-bearing courses share the same single fee value.
    """
    # Per-uni override: suppress for universities with a genuine flat-rate fee
    # (e.g. UEL charges £16,020 for every undergraduate course — not a bug).
    if uni_config is not None:
        try:
            if getattr(uni_config.extraction.staging, "skip_duplicate_fee_check", False):
                return []
        except AttributeError:
            pass
    issues: list[QualityIssue] = []
    fee_to_courses: dict[float, list[str]] = defaultdict(list)
    for payload, _url in payloads_with_urls:
        fee = payload.get("international_fee")
        if fee is None:
            continue
        try:
            fee_val = float(fee)
            if fee_val > 0:
                name = payload.get("course_name") or payload.get("name") or "?"
                fee_to_courses[fee_val].append(name)
        except (TypeError, ValueError):
            pass

    total_with_fee = sum(len(v) for v in fee_to_courses.values())
    if total_with_fee < 5:
        return issues  # Not enough data to detect duplicates reliably.

    for fee_val, course_names in sorted(fee_to_courses.items()):
        count = len(course_names)
        pct = count / max(total_with_fee, 1)
        if pct >= 0.75:
            sample = ", ".join(course_names[:4]) + (" …" if len(course_names) > 4 else "")
            issues.append(
                QualityIssue(
                    severity="critical",
                    code="duplicate_fee_detected",
                    message=(
                        f"Fee ${fee_val:,.0f} appears on {count}/{total_with_fee} "
                        f"courses ({pct:.0%}) — likely a selector-scope bug. "
                        f"Affected: {sample}"
                    ),
                )
            )
    return issues


# ---------------------------------------------------------------------------
# Aggregate report
# ---------------------------------------------------------------------------

async def run_quality_checks(
    staged_results: list[dict[str, Any]],
    *,
    emit: EmitFn = None,
    uni_config: "UniConfig | None" = None,
) -> dict[str, Any]:
    """Run all quality checks over the staged course batch.

    Parameters
    ----------
    staged_results:
        List of result dicts as produced by the extraction pipeline.
        Each dict must have a ``"payload"`` key and may have a ``"url"`` key.

    emit:
        Async callable matching the orchestrator's ``emit(event, message, **kw)``
        signature.  When provided, issues are streamed to the live log as they
        are found and a summary table is emitted at the end.

    uni_config:
        Optional merged UniConfig for the current scrape job.  When provided,
        per-university settings such as ``campus_allowlist`` are applied.
        Sourced from the ``current_uni_config`` contextvar via ``get_uni_config()``.

    Returns
    -------
    dict with keys:
        total_courses       — number of courses checked
        total_issues        — total issue count
        critical            — count of critical issues
        warnings            — count of warning issues
        info                — count of info issues
        issues              — list of issue dicts (sorted severity → code → url)
        critical_urls       — set of URLs where at least one critical issue was found
    """
    all_issues: list[QualityIssue] = []
    payloads_with_urls: list[tuple[Payload, str]] = []

    # Extract campus allowlist and default fee currency from uni_config if provided.
    campus_allowlist: list[str] = []
    _default_currency: str = "AUD"
    _require_intl_fee: bool = True  # default: missing fee is CRITICAL
    if uni_config is not None:
        try:
            campus_allowlist = uni_config.extraction.campus_allowlist or []
        except AttributeError:
            pass
        try:
            _cfg_currency = uni_config.extraction.fees.default_currency
            if _cfg_currency:
                _default_currency = _cfg_currency.strip().upper()
        except AttributeError:
            pass
        try:
            # When staging.require_international_fee=false the operator has
            # acknowledged that fees may not be extractable (e.g. Cloudflare-
            # protected JS tabs).  Downgrade missing_international_fee from
            # CRITICAL to WARNING so those courses land in the review queue
            # instead of data_quality_failure.
            _require_intl_fee = uni_config.extraction.staging.require_international_fee
        except AttributeError:
            pass

    for r in staged_results:
        if not isinstance(r, dict):
            continue
        payload = r.get("payload") or r
        # Skip records that were rejected at the staging gate.
        # domestic_only and parser_error courses are intentionally excluded
        # from scraped_courses; flagging them for missing_international_fee /
        # missing_course_name etc. is noise that swamps CRITICAL counts and
        # obscures real data-quality problems in the courses that DID stage.
        if payload.get("domestic_only") or payload.get("parser_error"):
            continue
        url = r.get("url") or r.get("source_url") or ""
        payloads_with_urls.append((payload, url))
        course_issues = _check_course(
            payload, url,
            campus_allowlist=campus_allowlist or None,
            default_currency=_default_currency,
            require_international_fee=_require_intl_fee,
        )
        all_issues.extend(course_issues)

    # Duplicate checks are cross-course
    all_issues.extend(_check_duplicates(payloads_with_urls))
    all_issues.extend(_check_duplicate_fees(payloads_with_urls, uni_config=uni_config))

    # Sort by severity then code then url for deterministic output.
    all_issues.sort(
        key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), i.code, i.url)
    )

    counts: dict[str, int] = {"critical": 0, "warning": 0, "info": 0}
    for issue in all_issues:
        counts[issue.severity] = counts.get(issue.severity, 0) + 1

    # Build the set of URLs that have at least one critical issue so the
    # orchestrator can mark those scraped_courses rows as data_quality_failure.
    critical_urls: set[str] = {
        i.url for i in all_issues if i.severity == "critical" and i.url
    }

    report: dict[str, Any] = {
        "total_courses": len(payloads_with_urls),
        "total_issues": len(all_issues),
        **counts,
        "issues": [i.to_dict() for i in all_issues],
        "critical_urls": critical_urls,
    }

    if emit:
        await _emit_report(all_issues, counts, len(payloads_with_urls), emit)

    log.info(
        "[DATA QUALITY] %d course(s) checked — %d critical / %d warning / %d info",
        len(payloads_with_urls),
        counts.get("critical", 0),
        counts.get("warning", 0),
        counts.get("info", 0),
    )
    return report


async def _emit_report(
    issues: list[QualityIssue],
    counts: dict[str, int],
    total_courses: int,
    emit: EmitFn,
) -> None:
    """Stream quality issues to the live log."""
    n_critical = counts.get("critical", 0)
    n_warning = counts.get("warning", 0)
    n_info = counts.get("info", 0)

    header_level = "error" if n_critical else ("warn" if n_warning else "info")
    await emit(
        "status",
        f"[DATA QUALITY] {total_courses} course(s) checked — "
        f"{n_critical} critical / {n_warning} warning / {n_info} info",
        phase="complete",
        kind="data_quality_summary",
        critical=n_critical,
        warnings=n_warning,
        info=n_info,
        total_courses=total_courses,
        level=header_level,
    )

    # Emit critical issues individually so operators can see them in the log.
    for issue in issues:
        if issue.severity != "critical":
            continue
        await emit(
            "status",
            f"[DATA QUALITY][CRITICAL] {issue.code}: {issue.message}"
            + (f" | {issue.url}" if issue.url else ""),
            phase="complete",
            kind="data_quality_issue",
            severity=issue.severity,
            code=issue.code,
            course_name=issue.course_name,
            url=issue.url,
            level="error",
        )

    # Emit warning-level issues grouped by code to avoid log flooding.
    warn_by_code: dict[str, list[QualityIssue]] = defaultdict(list)
    for issue in issues:
        if issue.severity == "warning":
            warn_by_code[issue.code].append(issue)

    for code, group in sorted(warn_by_code.items()):
        count = len(group)
        sample = group[0]
        await emit(
            "status",
            f"[DATA QUALITY][WARN] {code} × {count}: {sample.message}"
            + (f" (and {count - 1} more)" if count > 1 else ""),
            phase="complete",
            kind="data_quality_issue",
            severity="warning",
            code=code,
            count=count,
            level="warn",
        )
