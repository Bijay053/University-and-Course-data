"""Quality Intelligence — Phase 5: root-cause diagnosis beyond a completeness number.

Instead of:
    Completeness: 68%

Produces:
    Field        Fill   Status   Diagnosis                          Action
    ─────────────────────────────────────────────────────────────────────────
    intl. fee    0.95   ✓
    entry req.   0.18   ✗ low    Often in PDFs / behind JS         Activate PDF extraction
    IELTS        1.00   ✓
    duration     0.82   ✓
    …

The ``build_quality_report`` function combines per-field fill rates (from
``ScrapedFieldEvidence``) with the university's probe result to generate
field-level diagnoses and actionable recommendations.
"""
from __future__ import annotations

from typing import Any

# ── Per-field knowledge base ──────────────────────────────────────────────────
# Keys match ScrapedFieldEvidence.field_key values.
# Each entry has:
#   diagnosis_low  — why this field is commonly empty
#   action_low     — recommended system response
#   diagnosis_zero — why this field might be entirely absent
#   critical       — True if missing field blocks auto-publish

_FIELD_KB: dict[str, dict[str, Any]] = {
    "international_fee": {
        "label": "International Fee",
        "critical": True,
        "diagnosis_low": "Fees may appear only in downloadable fee schedules or PDFs",
        "action_low": "Activate PDF extraction; check fee_page_url configuration",
        "diagnosis_zero": "No fee page configured or fees require authentication",
        "action_zero": "Add fee_page_url to university config; try PDF pipeline",
    },
    "other_requirement": {
        "label": "Entry Requirements",
        "critical": True,
        "diagnosis_low": (
            "Requirements often require JavaScript rendering or "
            "are buried in expandable sections"
        ),
        "action_low": "Enable browser pass; check requirements_page_url",
        "diagnosis_zero": "Requirements page not discovered or blocked",
        "action_zero": "Configure requirements_page_url; use browser strategy",
    },
    "english_test": {
        "label": "English Test Score",
        "critical": True,
        "diagnosis_low": (
            "IELTS/PTE scores vary per course and may be on a separate "
            "requirements page, not embedded in the course listing"
        ),
        "action_low": "Check if per-course requirements page is being fetched",
        "diagnosis_zero": "No English requirement data found — verify source page layout",
        "action_zero": "Enable requirements page crawl; check PTE host blocklist",
    },
    "academic_level": {
        "label": "Academic Level",
        "critical": False,
        "diagnosis_low": "Degree level label not in a machine-readable position",
        "action_low": "Review CSS selectors for degree level on this platform",
        "diagnosis_zero": "Degree classification absent from source — run repair pass",
        "action_zero": "Run repair_extractor to regenerate rules for degree level",
    },
    "academic_score": {
        "label": "Academic Score (GPA/WAM)",
        "critical": False,
        "diagnosis_low": "GPA requirements often embedded in body text, not structured data",
        "action_low": "Check if AI extraction is enabled for academic requirements",
        "diagnosis_zero": "University may not publish GPA cutoffs publicly",
        "action_zero": "Confirm source publishes GPA data; enable Gemini extraction",
    },
    "category": {
        "label": "Course Category / Discipline",
        "critical": False,
        "diagnosis_low": "Category classification not present in course listing pages",
        "action_low": "Enable AI-based category inference from course name/description",
        "diagnosis_zero": "No category data — this field is usually AI-inferred",
        "action_zero": "Enable Gemini call for category; check gemini_gate skip rule",
    },
    "study_mode": {
        "label": "Study Mode (Online/On-campus)",
        "critical": False,
        "diagnosis_low": "Delivery mode may be in a non-standard location or label",
        "action_low": "Expand mode_keywords list in YAML; check 'delivery method' patterns",
        "diagnosis_zero": "Mode not published — site may use non-standard terminology",
        "action_zero": "Add custom mode patterns to per-uni YAML",
    },
    "course_location": {
        "label": "Campus Location",
        "critical": False,
        "diagnosis_low": "Campus listed in structured data not yet covered by extractors",
        "action_low": "Check campus CSS selector patterns for this CMS platform",
        "diagnosis_zero": "University may be online-only with no physical campus listed",
        "action_zero": "Set default location in YAML if online-only",
    },
    "duration": {
        "label": "Course Duration",
        "critical": False,
        "diagnosis_low": "Duration in non-standard format (e.g. 'three years')",
        "action_low": "Enable AI duration extraction; check _DURATION_RE pattern coverage",
        "diagnosis_zero": "Duration absent — confirm source page includes it",
        "action_zero": "Verify source HTML; add custom duration patterns to YAML",
    },
    "intake_months": {
        "label": "Intake / Start Dates",
        "critical": False,
        "diagnosis_low": "Intake months may use session names (Autumn/Spring) not months",
        "action_low": "Check session→month mapping in per-uni YAML config",
        "diagnosis_zero": "No intake data — site may use rolling admissions",
        "action_zero": "Confirm source lists intake dates; add session→month mapping",
    },
    "description": {
        "label": "Course Description",
        "critical": False,
        "diagnosis_low": "Description extracted but truncated or failed quality check",
        "action_low": "Check min-length threshold and extraction CSS selector",
        "diagnosis_zero": "Description not found — verify content selector",
        "action_zero": "Update description CSS selector for this platform",
    },
    "degree_level": {
        "label": "Degree Level",
        "critical": True,
        "diagnosis_low": "Degree level not in a consistent position across pages",
        "action_low": "Review degree_level patterns; run repair_extractor",
        "diagnosis_zero": "Degree level absent — critical field; triggers CASCADE",
        "action_zero": "Run repair_extractor; check guards.py degree qualifier patterns",
    },
    "course_name": {
        "label": "Course Name",
        "critical": True,
        "diagnosis_low": "Course name extraction failing for some page layouts",
        "action_low": "Review title/h1 CSS selectors; check name-dedup logic",
        "diagnosis_zero": "No course names found — fundamental discovery issue",
        "action_zero": "Check discovery pipeline; review BFS/sitemap results",
    },
}

# Fill-rate thresholds
_GOOD_THRESHOLD = 0.80    # ≥80% = good
_WARN_THRESHOLD = 0.40    # 40–80% = warning
# < 40% = poor
# == 0.0 = zero (no data at all)


def _field_status(rate: float) -> str:
    if rate >= _GOOD_THRESHOLD:
        return "good"
    if rate >= _WARN_THRESHOLD:
        return "warning"
    if rate > 0.0:
        return "poor"
    return "zero"


def _diagnosis_for(field_key: str, rate: float) -> dict[str, str]:
    """Return the diagnosis + action for a given field and fill rate."""
    kb = _FIELD_KB.get(field_key)
    if kb is None:
        return {}
    if rate == 0.0:
        return {
            "diagnosis": kb.get("diagnosis_zero", ""),
            "action": kb.get("action_zero", ""),
        }
    return {
        "diagnosis": kb.get("diagnosis_low", ""),
        "action": kb.get("action_low", ""),
    }


# ── Probe signal → extra diagnosis hints ─────────────────────────────────────

def _probe_hints(probe_summary: dict[str, Any] | None) -> list[str]:
    """Derive high-level platform hints from the stored probe result."""
    if not probe_summary:
        return []
    hints: list[str] = []
    if probe_summary.get("is_cloudflare_blocked"):
        hints.append("Site is Cloudflare-protected — browser or Wayback strategy required")
    if probe_summary.get("is_js_spa"):
        fw = probe_summary.get("spa_framework") or "unknown framework"
        hints.append(
            f"JS SPA ({fw}) — static HTML extraction will miss dynamic content"
        )
    cms = probe_summary.get("cms_platform")
    if cms:
        hints.append(f"CMS identified: {cms} — platform-specific patterns may be available")
    if not probe_summary.get("has_sitemap"):
        hints.append("No sitemap found — BFS discovery may miss some course pages")
    apis = probe_summary.get("detected_apis") or []
    if apis:
        names = ", ".join(a.get("provider", "?") for a in apis[:3])
        hints.append(
            f"Search API detected ({names}) — API extraction may give better coverage"
        )
    return hints


# ── Public API ────────────────────────────────────────────────────────────────

def build_quality_report(
    fill_rates: dict[str, dict[str, Any]],
    probe_summary: dict[str, Any] | None = None,
    overall_avg: float = 0.0,
) -> dict[str, Any]:
    """Build a structured quality report from fill-rate data and probe signals.

    Parameters
    ----------
    fill_rates:
        Dict returned by ``get_field_fill_rates``: ``{field_key: {filled, total, rate}}``.
    probe_summary:
        The university's stored ``probe_result`` dict (may be None for un-probed unis).
    overall_avg:
        Pre-computed overall average fill rate.

    Returns
    -------
    dict with keys:
        overall_pct        — 0–100 integer (overall completeness %)
        overall_status     — "good" | "warning" | "poor" | "zero"
        fields             — per-field breakdown with status + optional diagnosis
        issues             — list of fields with poor/zero fill + their diagnosis
        platform_hints     — high-level hints derived from probe signals
        recommended_actions — deduplicated action list ordered by severity
    """
    fields: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []

    for field_key, rate_data in fill_rates.items():
        rate = float(rate_data.get("rate", 0.0))
        status = _field_status(rate)
        kb = _FIELD_KB.get(field_key, {})
        entry: dict[str, Any] = {
            "label": kb.get("label", field_key),
            "fill_rate": round(rate, 3),
            "filled": rate_data.get("filled", 0),
            "total": rate_data.get("total", 0),
            "status": status,
            "critical": bool(kb.get("critical", False)),
        }
        if status in ("poor", "zero", "warning") and field_key in _FIELD_KB:
            diag = _diagnosis_for(field_key, rate)
            entry.update(diag)
            if status in ("poor", "zero"):
                issues.append({
                    "field": field_key,
                    "label": kb.get("label", field_key),
                    "fill_rate": round(rate, 3),
                    "status": status,
                    "critical": bool(kb.get("critical", False)),
                    "diagnosis": diag.get("diagnosis", ""),
                    "action": diag.get("action", ""),
                })

        fields[field_key] = entry

    # Sort issues: critical first, then zero > poor
    issues.sort(key=lambda x: (not x["critical"], x["fill_rate"]))

    # Deduplicated action list
    seen_actions: set[str] = set()
    recommended_actions: list[str] = []
    for issue in issues:
        action = issue.get("action", "")
        if action and action not in seen_actions:
            seen_actions.add(action)
            recommended_actions.append(action)

    platform_hints = _probe_hints(probe_summary)

    overall_pct = round(overall_avg * 100)
    overall_status = _field_status(overall_avg)

    return {
        "overall_pct": overall_pct,
        "overall_status": overall_status,
        "fields": fields,
        "issues": issues,
        "platform_hints": platform_hints,
        "recommended_actions": recommended_actions,
    }
