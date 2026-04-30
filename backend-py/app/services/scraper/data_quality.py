"""Scraper data-quality validation module.

Runs AFTER the staging loop and BEFORE the DONE event is emitted.  Inspects
every staged course payload to surface data-quality issues early â before they
reach operators or the publish queue.

Each issue is classified by severity:
    "critical"  â data is almost certainly wrong or missing; blocks publish.
    "warning"   â data may be wrong or incomplete; flags for review.
    "info"      â observation worth noting; does not block anything.

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
from typing import Any, Awaitable, Callable

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

# Implausible fee boundaries (AUD)
_FEE_MIN = 500.0
_FEE_MAX = 250_000.0

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
    separated by â¥ 1 full IELTS band-width from any plausible equivalence.
    This means TOEFL 80 for an IELTS 5.5 course (above ETS official but used
    by many Australian universities) is NOT flagged, but TOEFL 95 for an
    IELTS 6.0 course (which corresponds to IELTS 7.0-7.5) IS flagged.

    All issues are "warning" severity â they flag for human review without
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
        return issues  # no anchor â nothing to cross-check against

    toefl = _num("toefl_overall")
    pte = _num("pte_overall")
    duolingo = _num("duolingo_overall")
    cambridge = _num("cambridge_overall")

    def _add(code: str, msg: str) -> None:
        issues.append(QualityIssue("warning", code, msg, url=url, course_name=name))

    # ââ IELTS vs TOEFL ââââââââââââââââââââââââââââââââââââââââââââââââââ
    # TOEFL 85+ â IELTS 6.5+; IELTS â¤ 6.0 + TOEFL â¥ 85 is a mismatch.
    # TOEFL 75- â IELTS â¤ 5.5; IELTS â¥ 7.0 + TOEFL â¤ 75 is a mismatch.
    if toefl is not None:
        if ielts <= 6.0 and toefl >= 85:
            _add(
                "english_coherence_toefl",
                f"IELTS {ielts} + TOEFL {toefl} is inconsistent: "
                f"TOEFL {toefl:.0f} corresponds to IELTS â¥ 6.5. "
                f"One value is likely sourced from a different level or hallucinated.",
            )
        elif ielts >= 7.0 and toefl <= 75:
            _add(
                "english_coherence_toefl",
                f"IELTS {ielts} + TOEFL {toefl} is inconsistent: "
                f"TOEFL {toefl:.0f} corresponds to IELTS â¤ 5.5. "
                f"One value is likely sourced from a different level or hallucinated.",
            )

    # ââ IELTS vs PTE ââââââââââââââââââââââââââââââââââââââââââââââââââââ
    # PTE 65+ â IELTS 7.0+; IELTS â¤ 6.0 + PTE â¥ 65 is a mismatch.
    # PTE 45- â IELTS â¤ 5.5; IELTS â¥ 7.0 + PTE â¤ 45 is a mismatch.
    if pte is not None:
        if ielts <= 6.0 and pte >= 65:
            _add(
                "english_coherence_pte",
                f"IELTS {ielts} + PTE {pte} is inconsistent: "
                f"PTE {pte:.0f} corresponds to IELTS â¥ 7.0. "
                f"One value is likely sourced from a different level or hallucinated.",
            )
        elif ielts >= 7.0 and pte <= 45:
            _add(
                "english_coherence_pte",
                f"IELTS {ielts} + PTE {pte} is inconsistent: "
                f"PTE {pte:.0f} corresponds to IELTS â¤ 5.0. "
                f"One value is likely sourced from a different level or hallucinated.",
            )

    # ââ IELTS vs Duolingo âââââââââââââââââââââââââââââââââââââââââââââââ
    # Duolingo 115+ â IELTS 7.0+; IELTS â¤ 6.0 + DET â¥ 115 is a mismatch.
    # Duolingo 95-  â IELTS â¤ 5.5; IELTS â¥ 7.5 + DET â¤ 95 is a mismatch.
    if duolingo is not None:
        if ielts <= 6.0 and duolingo >= 115:
            _add(
                "english_coherence_duolingo",
                f"IELTS {ielts} + Duolingo {duolingo} is inconsistent: "
                f"Duolingo {duolingo:.0f} corresponds to IELTS â¥ 7.0. "
                f"Duolingo value may be hallucinated or from wrong level cache.",
            )
        elif ielts >= 7.5 and duolingo <= 95:
            _add(
                "english_coherence_duolingo",
                f"IELTS {ielts} + Duolingo {duolingo} is inconsistent: "
                f"Duolingo {duolingo:.0f} corresponds to IELTS â¤ 5.5. "
                f"Duolingo value may be hallucinated or from wrong level cache.",
            )

    # ââ IELTS vs Cambridge (CAE) âââââââââââââââââââââââââââââââââââââââââ
    # Cambridge 176+ â IELTS 7.0+; IELTS â¤ 6.0 + CAE â¥ 176 is a mismatch.
    # Cambridge 162- â IELTS â¤ 5.5; IELTS â¥ 7.0 + CAE â¤ 162 is a mismatch.
    # Note: VIT shows CAE 176 on vocational courses with IELTS 5.5 â this fires
    # on those rows intentionally, since 176 is the C1 Advanced threshold (IELTS 7.0).
    if cambridge is not None:
        if ielts <= 6.0 and cambridge >= 176:
            _add(
                "english_coherence_cambridge",
                f"IELTS {ielts} + Cambridge {cambridge} is inconsistent: "
                f"CAE {cambridge:.0f} corresponds to IELTS â¥ 7.0 (C1 Advanced threshold). "
                f"Cambridge value may be a university-wide default that doesn't apply to this level.",
            )
        elif ielts >= 7.0 and cambridge <= 162:
            _add(
                "english_coherence_cambridge",
                f"IELTS {ielts} + Cambridge {cambridge} is inconsistent: "
                f"CAE {cambridge:.0f} corresponds to IELTS â¤ 5.5. "
                f"One value is likely sourced from a different level.",
            )

    return issues


def _check_course(payload: Payload, url: str) -> list[QualityIssue]:
    """Return a list of quality issues for one staged course payload."""
    issues: list[QualityIssue] = []
    name = payload.get("course_name") or payload.get("name") or "?"

    def add(severity: str, code: str, msg: str) -> None:
        issues.append(QualityIssue(severity, code, msg, url=url, course_name=name))

    # ââ 1. Course title âââââââââââââââââââââââââââââââââââââââââââââââââââ
    if not name or name == "?":
        add("critical", "missing_course_name", "Course name is blank.")
    elif _GENERIC_TITLE_RE.match(name):
        add("critical", "generic_course_title",
            f"Title looks like a category page, not a specific course: {name!r}")
    elif len(name) < 8:
        add("warning", "suspiciously_short_title",
            f"Course title is very short ({len(name)} chars): {name!r}")

    # ââ 2. Fee âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    intl_fee = payload.get("international_fee")
    has_central_fee = payload.get("has_central_fee_page")
    if intl_fee is None:
        if not has_central_fee:
            add("critical", "missing_international_fee",
                "No international fee found and no central fee page flag set.")
        else:
            add("warning", "missing_international_fee_central_page",
                "International fee absent â marked for central fee page review.")
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
        except (TypeError, ValueError):
            add("warning", "non_numeric_fee",
                f"International fee value is not numeric: {intl_fee!r}")

    # ââ 3. IELTS / English requirement âââââââââââââââââââââââââââââââââââ
    has_any_english = any(
        payload.get(k) is not None
        for k in (
            "ielts_overall", "ielts_reading", "ielts_writing",
            "ielts_listening", "ielts_speaking",
            "pte_overall", "toefl_overall", "cambridge_overall",
        )
    )
    if not has_any_english:
        add("warning", "missing_english_requirement",
            "No English language test score found (IELTS / PTE / TOEFL / CAE).")

    # ââ 3a. Test-accepted flag violations ââââââââââââââââââââââââââââââââ
    # If a university explicitly does not accept a test (*_accepted=False)
    # but the pipeline wrote a score for it anyway (from vision, sibling cache,
    # etc.), that is a definite pipeline error â the score cannot be correct.
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
                f"{_flag_key}=False â university does not accept this test. "
                f"Score is likely hallucinated (vision or sibling cache). Discard it.",
            )

    # ââ 3b. Cross-field English coherence ââââââââââââââââââââââââââââââââ
    # Each test score should be broadly consistent with the others â if
    # the extractor wrote each field from a different source (regex, vision,
    # sibling cache) without cross-checking, impossible combinations can
    # appear (e.g. IELTS 6.0 + TOEFL 95 = two different admission levels).
    #
    # Thresholds are permissive (not the strict ETS official equivalence)
    # because Australian universities sometimes set their own stricter tables
    # (e.g. TOEFL 80 for IELTS 5.5 courses, which is above the ETS equivalent
    # of 72 but still within a defensible margin). We only flag combinations
    # that are clearly impossible â separated by â¥ 1 IELTS band-width.
    issues.extend(_check_english_coherence(payload, url, name))

    # ââ 4. Duration âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
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
                        f"(0 < duration â¤ {_DURATION_YEAR_MAX}).")
            elif duration_term in ("month", "months"):
                if dur_val <= 0 or dur_val > _DURATION_MONTH_MAX:
                    add("warning", "suspicious_duration",
                        f"Duration {dur_val} month(s) is outside the expected range "
                        f"(0 < duration â¤ {_DURATION_MONTH_MAX}).")
            elif duration_term in ("week", "weeks"):
                if dur_val <= 0 or dur_val > _DURATION_WEEK_MAX:
                    add("warning", "suspicious_duration",
                        f"Duration {dur_val} week(s) is outside the expected range "
                        f"(0 < duration â¤ {_DURATION_WEEK_MAX}).")
        except (TypeError, ValueError):
            add("warning", "non_numeric_duration",
                f"Duration value is not numeric: {duration!r}")

    # ââ 5. Intake months âââââââââââââââââââââââââââââââââââââââââââââââââ
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
                f"intake_months has {len(intake_months)} entries â likely extraction noise.")

    # ââ 6. Location âââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    location = payload.get("course_location") or ""
    if not location.strip():
        add("info", "missing_location",
            "No course location extracted.")
    elif _JUNK_LOCATION_RE.search(location):
        add("warning", "suspicious_location",
            f"Location looks like footer/admin text rather than a campus name: {location!r}")

    # ââ 7. Study mode ââââââââââââââââââââââââââââââââââââââââââââââââââââ
    study_mode = payload.get("study_mode") or ""
    if not study_mode.strip():
        add("info", "missing_study_mode",
            "Study mode not extracted â will show blank in Review UI.")

    # ââ 8. Degree level âââââââââââââââââââââââââââââââââââââââââââââââââââ
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
                        + (" (â¦)" if len(urls) > 3 else "")
                    ),
                )
            )
    return issues


def _check_duplicate_fees(payloads_with_urls: list[tuple[Payload, str]]) -> list[QualityIssue]:
    """Detect repeated fee values across courses â a strong indicator of a
    selector-scope reuse bug (the same DOM element being scraped for every
    course page). Fires when:
      â¢ At least 5 courses have a fee, AND
      â¢ â¥ 75% of fee-bearing courses share the same single fee value.
    """
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
            sample = ", ".join(course_names[:4]) + (" â¦" if len(course_names) > 4 else "")
            issues.append(
                QualityIssue(
                    severity="critical",
                    code="duplicate_fee_detected",
                    message=(
                        f"Fee ${fee_val:,.0f} appears on {count}/{total_with_fee} "
                        f"courses ({pct:.0%}) â likely a selector-scope bug. "
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

    Returns
    -------
    dict with keys:
        total_courses  â number of courses checked
        total_issues   â total issue count
        critical       â count of critical issues
        warnings       â count of warning issues
        info           â count of info issues
        issues         â list of issue dicts (sorted severity â code â url)
    """
    all_issues: list[QualityIssue] = []
    payloads_with_urls: list[tuple[Payload, str]] = []

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
        course_issues = _check_course(payload, url)
        all_issues.extend(course_issues)

    # Duplicate checks are cross-course
    all_issues.extend(_check_duplicates(payloads_with_urls))
    all_issues.extend(_check_duplicate_fees(payloads_with_urls))

    # Sort by severity then code then url for deterministic output.
    all_issues.sort(
        key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), i.code, i.url)
    )

    counts: dict[str, int] = {"critical": 0, "warning": 0, "info": 0}
    for issue in all_issues:
        counts[issue.severity] = counts.get(issue.severity, 0) + 1

    report: dict[str, Any] = {
        "total_courses": len(payloads_with_urls),
        "total_issues": len(all_issues),
        **counts,
        "issues": [i.to_dict() for i in all_issues],
    }

    if emit:
        await _emit_report(all_issues, counts, len(payloads_with_urls), emit)

    log.info(
        "[DATA QUALITY] %d course(s) checked â %d critical / %d warning / %d info",
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
        f"[DATA QUALITY] {total_courses} course(s) checked â "
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
            f"[DATA QUALITY][WARN] {code} Ã {count}: {sample.message}"
            + (f" (and {count - 1} more)" if count > 1 else ""),
            phase="complete",
            kind="data_quality_issue",
            severity="warning",
            code=code,
            count=count,
            level="warn",
        )
