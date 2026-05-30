"""Phase 7: Autonomous Quality Action Dispatcher.

After every scrape completes with average completeness in the 70–84 % gap
(above CASCADE's repair threshold but below the 85 % auto-publish gate),
this module maps weak fields → safe, bounded, logged recovery actions.

Flow
----
  fill_rates (per-field) → weak fields → action type map →
  safety checks → run inline (PDF extraction) or dispatch Celery
  (repair_extractor / browser_retry) → log result

Safety guarantees
-----------------
* Never overwrites existing values with fill_rate ≥ 80 %.
* Each ActionType dispatched at most once per run (dedup set).
* Celery dispatches capped at ``_MAX_CELERY_DISPATCHES`` (2) per run.
* REPAIR_EXTRACTOR skipped when CASCADE already fired it (arg flag).
* MANUAL_REVIEW returned when no safe auto-action exists.
* Entire dispatcher wrapped in try/except in the orchestrator — never
  fails the job.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


# ── Action taxonomy ───────────────────────────────────────────────────────────

class ActionType(str, Enum):
    PDF_EXTRACTION   = "pdf_extraction"   # Extract missing data from discovered PDFs (inline)
    API_PROMOTION    = "api_promotion"    # Promote API field mapping — handled by Phase 4B
    REPAIR_EXTRACTOR = "repair_extractor" # AI-regenerate CSS/XPath rules (Celery)
    BROWSER_RETRY    = "browser_retry"    # Retry with Playwright browser mode (Celery)
    MANUAL_REVIEW    = "manual_review"    # No safe auto-action — flag for human


@dataclass
class QualityAction:
    action_type: ActionType
    target_fields: list[str]
    reason: str
    executed: bool = False
    skipped_reason: str = ""
    result: str = ""
    courses_improved: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type.value,
            "target_fields": self.target_fields,
            "reason": self.reason,
            "executed": self.executed,
            "skipped_reason": self.skipped_reason,
            "result": self.result,
            "courses_improved": self.courses_improved,
        }


@dataclass
class DispatchResult:
    overall_before: float = 0.0
    overall_after: float = 0.0
    actions: list[QualityAction] = field(default_factory=list)
    inline_improved: int = 0
    celery_dispatched: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_before": round(self.overall_before, 3),
            "overall_after": round(self.overall_after, 3),
            "actions": [a.to_dict() for a in self.actions],
            "inline_improved": self.inline_improved,
            "celery_dispatched": self.celery_dispatched,
        }


# ── Field → action map ────────────────────────────────────────────────────────

# Maps field_key → (ActionType, human-readable reason for logs)
_FIELD_ACTION_MAP: dict[str, tuple[ActionType, str]] = {
    # PDF extraction — fee and requirement data often only in downloadable PDFs
    "international_fee":  (ActionType.PDF_EXTRACTION,   "Fee schedules often in downloadable PDFs"),
    "other_requirement":  (ActionType.PDF_EXTRACTION,   "Entry requirements often in PDFs or behind JS"),
    "english_test":       (ActionType.PDF_EXTRACTION,   "IELTS/PTE scores often in requirements PDF"),
    "academic_score":     (ActionType.PDF_EXTRACTION,   "GPA/WAM requirements often in requirements PDF"),
    # Extractor repair — structural fields need AI rule regeneration
    "degree_level":       (ActionType.REPAIR_EXTRACTOR, "Critical: degree level rules need regeneration"),
    "course_name":        (ActionType.REPAIR_EXTRACTOR, "Critical: course name extraction failing"),
    "academic_level":     (ActionType.REPAIR_EXTRACTOR, "Degree level classification needs rule repair"),
    "category":           (ActionType.REPAIR_EXTRACTOR, "Category inference needs Gemini or rule repair"),
    "description":        (ActionType.REPAIR_EXTRACTOR, "Description selector needs repair for this platform"),
    "study_mode":         (ActionType.REPAIR_EXTRACTOR, "Mode extraction failing; repair delivery-mode patterns"),
    "duration":           (ActionType.REPAIR_EXTRACTOR, "Duration patterns need repair for this platform"),
    "course_location":    (ActionType.REPAIR_EXTRACTOR, "Campus/location selector needs repair"),
    "intake_months":      (ActionType.REPAIR_EXTRACTOR, "Intake month patterns need repair"),
}

# Fill-rate thresholds
_GOOD_FILL = 0.80        # Fields at ≥80 % are already good — skip
_ACT_THRESHOLD = 0.85    # Only dispatch P7 when overall avg < 85 %
_WEAK_FILL = 0.40        # Fields below 40 % warrant action

# Budget caps — prevent runaway Celery dispatches
_MAX_CELERY_DISPATCHES = 2   # max Celery tasks dispatched per run

# Priority order for execution
_ACTION_PRIORITY = [
    ActionType.PDF_EXTRACTION,
    ActionType.API_PROMOTION,
    ActionType.REPAIR_EXTRACTOR,
    ActionType.BROWSER_RETRY,
    ActionType.MANUAL_REVIEW,
]

# Critical fields get sorted to the front of the weak-field list
_CRITICAL_FIELDS = {
    "degree_level", "course_name", "international_fee",
    "other_requirement", "english_test",
}


# ── Safety checks ─────────────────────────────────────────────────────────────

def _skip_reason(
    action_type: ActionType,
    min_fill: float,
    already_dispatched: set[ActionType],
    celery_count: int,
    cascade_repair_fired: bool,
) -> str | None:
    """Return a human-readable skip reason, or None when the action is safe to run."""
    if min_fill >= _GOOD_FILL:
        return f"fill_rate={min_fill:.0%} already good (≥{_GOOD_FILL:.0%})"
    if action_type in already_dispatched:
        return f"{action_type.value} already dispatched this run"
    if action_type in (ActionType.REPAIR_EXTRACTOR, ActionType.BROWSER_RETRY):
        if celery_count >= _MAX_CELERY_DISPATCHES:
            return f"Celery budget exhausted (max {_MAX_CELERY_DISPATCHES} per run)"
        if action_type == ActionType.REPAIR_EXTRACTOR and cascade_repair_fired:
            return "CASCADE already dispatched repair_extractor — skipping duplicate"
    return None


# ── Inline action: PDF extraction ─────────────────────────────────────────────

async def _run_pdf_extraction(
    job_id: str,
    target_fields: list[str],
    scrape_url: str,
    uni_country: str,
    uni_scrape_config: dict,
    db: Any,
    emit: Any,
) -> tuple[int, str]:
    """Discover PDFs, extract data for weak fields, backfill scraped_courses.

    Returns (courses_improved, detail_string).
    Never raises — all errors are caught and returned as detail strings.
    """
    from app.services.scraper.pipelines.university_pdfs import load_university_pdf_data
    from sqlalchemy import text as _sql

    try:
        # Use cached discovered PDFs when available
        _ac = uni_scrape_config.get("auto_config") or {}
        _pdfs = list(_ac.get("_discovered_pdfs") or [])

        if not _pdfs and scrape_url:
            from app.services.scraper.pdf_link_discoverer import (
                discover_pdf_links_for_university as _disc,
            )
            _raw = await _disc(scrape_url, emit=emit)
            _pdfs = [lnk.to_dict() for lnk in _raw[:10]]

        # Inject top-scoring discovered PDFs into a temp config
        _cfg = dict(uni_scrape_config)
        _pages = dict(_cfg.get("uniPages") or {})
        _cfg["uniPages"] = _pages
        for item in _pdfs:
            cat = (item.get("best_category") or "").strip()
            url = (item.get("url") or "").strip()
            if not url:
                continue
            if cat == "fee_schedule" and not _pages.get("feesPdf"):
                _pages["feesPdf"] = url
            elif cat == "entry_requirements" and not _pages.get("requirementsPdf"):
                _pages["requirementsPdf"] = url

        pdf_data = await load_university_pdf_data(_cfg, uni_country, emit=emit)
        if not pdf_data:
            return 0, "no PDF data extracted"

        improved = 0
        parts: list[str] = []

        # international_fee
        if "international_fee" in target_fields:
            fee_val = (pdf_data.get("fee") or {}).get("international_fee")
            if fee_val:
                r = await db.execute(
                    _sql("UPDATE scraped_courses SET international_fee = :v"
                         " WHERE scrape_job_id = :j AND international_fee IS NULL"),
                    {"v": fee_val, "j": job_id},
                )
                n = getattr(r, "rowcount", 0) or 0
                if n:
                    improved += n
                    parts.append(f"international_fee+{n}")

        # other_requirement
        if "other_requirement" in target_fields:
            er = pdf_data.get("entry_requirements") or {}
            if er:
                from app.services.scraper.entry_req_extractor import EntryRequirement as _ER
                er_text = _ER.from_dict(er).to_summary_text()
                if er_text:
                    r = await db.execute(
                        _sql("UPDATE scraped_courses SET other_requirement = :v"
                             " WHERE scrape_job_id = :j"
                             "   AND (other_requirement IS NULL OR other_requirement = '')"),
                        {"v": er_text[:500], "j": job_id},
                    )
                    n = getattr(r, "rowcount", 0) or 0
                    if n:
                        improved += n
                        parts.append(f"other_requirement+{n}")

        # english_test / ielts_overall
        if "english_test" in target_fields:
            eng = pdf_data.get("english") or {}
            er = pdf_data.get("entry_requirements") or {}
            ielts = eng.get("ielts_overall") or er.get("ielts_overall")
            if ielts:
                r = await db.execute(
                    _sql("UPDATE scraped_courses SET ielts_overall = :v"
                         " WHERE scrape_job_id = :j AND ielts_overall IS NULL"),
                    {"v": float(ielts), "j": job_id},
                )
                n = getattr(r, "rowcount", 0) or 0
                if n:
                    improved += n
                    parts.append(f"ielts_overall+{n}")

        # academic_score (GPA / ATAR)
        if "academic_score" in target_fields:
            er = pdf_data.get("entry_requirements") or {}
            gpa = er.get("gpa_min")
            atar = er.get("atar_min")
            if gpa or atar:
                score_text = (
                    f"GPA {gpa}/{er.get('gpa_scale', '?')}" if gpa else f"ATAR {atar}"
                )
                r = await db.execute(
                    _sql("UPDATE scraped_courses SET academic_score = :v"
                         " WHERE scrape_job_id = :j"
                         "   AND (academic_score IS NULL OR academic_score = '')"),
                    {"v": score_text, "j": job_id},
                )
                n = getattr(r, "rowcount", 0) or 0
                if n:
                    improved += n
                    parts.append(f"academic_score+{n}")

        if improved:
            await db.commit()

        return improved, "; ".join(parts) if parts else "no matching empty rows"

    except Exception as exc:  # noqa: BLE001
        log.warning("[P7·PDF] extraction error: %s", exc)
        return 0, f"error: {exc}"


# ── Celery dispatch helpers ───────────────────────────────────────────────────

def _dispatch_repair_extractor(university_id: int, job_id: str) -> bool:
    """Dispatch repair_extractor Celery task.  Returns True if dispatched."""
    try:
        from app.tasks.scrape_tasks import repair_extractor as _task
        _task.delay(
            university_id,
            scrape_run_id=job_id,
            triggered_by="quality_action_dispatcher",
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("[P7] repair_extractor dispatch failed: %s", exc)
        return False


def _dispatch_browser_retry(university_id: int, job_id: str) -> bool:
    """Queue a new scrape forcing browser strategy.  Returns True if dispatched."""
    try:
        from app.tasks.scrape_tasks import run_scrape_task as _scrape
        _scrape.delay(
            university_id=university_id,
            triggered_by="quality_action_dispatcher:browser_retry",
            force_strategy="browser",
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("[P7] browser_retry dispatch failed: %s", exc)
        return False


# ── Avg completeness helper ───────────────────────────────────────────────────

async def get_avg_completeness(job_id: str, db: Any) -> float:
    """Fast SQL average of completeness for staged courses of this job.

    Returns a value in the 0.0–1.0 range.  The ``scraped_courses.completeness``
    column stores 0–100 integers (set by ``compute_completeness`` in
    completeness.py), so we divide by 100 here so callers can compare against
    the 0–1 thresholds used throughout this module (_ACT_THRESHOLD, _WEAK_FILL).
    """
    from sqlalchemy import text as _sql
    row = (await db.execute(
        _sql("SELECT AVG(completeness) FROM scraped_courses"
             " WHERE scrape_job_id = :j AND completeness IS NOT NULL"),
        {"j": job_id},
    )).scalar()
    return round(float(row) / 100, 4) if row is not None else 0.0


# ── Public entry point ────────────────────────────────────────────────────────

async def dispatch_quality_actions(
    university_id: int,
    job_id: str,
    fill_rates: dict[str, float],
    scrape_url: str,
    uni_country: str,
    uni_scrape_config: dict,
    db: Any,
    emit: Any = None,
    cascade_repair_fired: bool = False,
    overall_avg: float | None = None,
) -> DispatchResult:
    """Dispatch recovery actions for a scrape run whose completeness is below 85 %.

    Parameters
    ----------
    fill_rates:
        ``{field_key: rate_0_to_1}`` from ``compute_field_fill_rates``.
    cascade_repair_fired:
        True when the CASCADE path already dispatched ``repair_extractor``.
        Prevents duplicate Celery task for the same run.
    overall_avg:
        Pre-computed average if caller already has it; re-queried from DB
        when None.
    """
    result = DispatchResult()
    emit_fn = emit if callable(emit) else (lambda *a, **kw: None)

    if overall_avg is None:
        overall_avg = await get_avg_completeness(job_id, db)
    result.overall_before = overall_avg

    if overall_avg >= _ACT_THRESHOLD:
        log.debug(
            "[P7] avg=%.1f%% at threshold — no dispatch needed for job %s",
            overall_avg * 100, job_id,
        )
        return result

    # Identify weak fields (below threshold, in the knowledge base)
    weak: list[tuple[str, float]] = sorted(
        (
            (fk, fr)
            for fk, fr in fill_rates.items()
            if fr < _WEAK_FILL and fk in _FIELD_ACTION_MAP
        ),
        key=lambda x: (x[0] not in _CRITICAL_FIELDS, x[1]),
    )

    if not weak:
        log.info("[P7] no weak fields for job %s — skipping", job_id)
        return result

    # Group weak fields by action type (preserving priority order within each group)
    by_action: dict[ActionType, list[str]] = {}
    for fk, _ in weak:
        at, _ = _FIELD_ACTION_MAP[fk]
        by_action.setdefault(at, []).append(fk)

    already_dispatched: set[ActionType] = set()
    celery_count = 0

    for action_type in _ACTION_PRIORITY:
        if action_type not in by_action:
            continue
        target_fields = by_action[action_type]
        reason = _FIELD_ACTION_MAP[target_fields[0]][1]
        min_fill = min(fill_rates.get(f, 0.0) for f in target_fields)

        skip = _skip_reason(
            action_type, min_fill, already_dispatched, celery_count, cascade_repair_fired,
        )
        action = QualityAction(
            action_type=action_type,
            target_fields=target_fields,
            reason=reason,
        )

        if skip:
            action.skipped_reason = skip
            result.actions.append(action)
            log.debug("[P7] skipping %s: %s", action_type.value, skip)
            continue

        # ── Execute ──────────────────────────────────────────────────────────
        if action_type == ActionType.PDF_EXTRACTION:
            n, detail = await _run_pdf_extraction(
                job_id, target_fields, scrape_url, uni_country,
                uni_scrape_config, db, emit_fn,
            )
            action.executed = True
            action.courses_improved = n
            action.result = detail
            result.inline_improved += n
            already_dispatched.add(action_type)
            log.info(
                "[P7·PDF] uni=%s job=%s fields=%s improved=%d detail=%r",
                university_id, job_id, target_fields, n, detail,
            )

        elif action_type == ActionType.API_PROMOTION:
            # Phase 4B already handles API promotion inline — just log
            action.executed = False
            action.skipped_reason = "handled by Phase 4B API promotion (already ran)"

        elif action_type == ActionType.REPAIR_EXTRACTOR:
            ok = _dispatch_repair_extractor(university_id, job_id)
            action.executed = ok
            action.result = "repair_extractor queued" if ok else "dispatch failed"
            if ok:
                celery_count += 1
                result.celery_dispatched.append("repair_extractor")
            already_dispatched.add(action_type)
            log.info(
                "[P7·REPAIR] uni=%s job=%s fields=%s dispatched=%s",
                university_id, job_id, target_fields, ok,
            )

        elif action_type == ActionType.BROWSER_RETRY:
            ok = _dispatch_browser_retry(university_id, job_id)
            action.executed = ok
            action.result = "browser scrape queued" if ok else "dispatch failed"
            if ok:
                celery_count += 1
                result.celery_dispatched.append("browser_retry")
            already_dispatched.add(action_type)
            log.info(
                "[P7·BROWSER] uni=%s job=%s fields=%s dispatched=%s",
                university_id, job_id, target_fields, ok,
            )

        elif action_type == ActionType.MANUAL_REVIEW:
            action.executed = False
            action.result = f"no safe auto-action for: {target_fields}"
            log.info(
                "[P7·MANUAL] uni=%s job=%s fields=%s flagged for human review",
                university_id, job_id, target_fields,
            )

        result.actions.append(action)

    # Re-measure after inline improvements
    if result.inline_improved > 0:
        result.overall_after = await get_avg_completeness(job_id, db)
    else:
        result.overall_after = overall_avg

    gain = (result.overall_after - result.overall_before) * 100
    log.info(
        "[P7] done uni=%s job=%s: avg %.1f%%→%.1f%% (Δ+%.1f%%) "
        "inline_improved=%d celery=%s actions=%d",
        university_id, job_id,
        result.overall_before * 100, result.overall_after * 100, gain,
        result.inline_improved, result.celery_dispatched, len(result.actions),
    )
    return result
