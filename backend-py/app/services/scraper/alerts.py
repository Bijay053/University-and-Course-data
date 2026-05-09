"""Priority 5 — Component 3: alert evaluator + rules engine.

After every scrape run completes (immediately after compute_run_metrics),
``evaluate_run_alerts`` checks the run's metrics against:

  1. Per-field fill-rate vs baseline floor (or global default if no baseline)
  2. Method-distribution quality rules (vision dominance, sibling cache
     dominance, AI fallback dominance, Facebook/icon source URLs)
  3. Trend detection — significant drop vs trailing 5-run average

All alert rows are persisted to ``scrape_run_alerts`` and returned so the
delivery layer can send critical ones to Slack / email.
"""
from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scrape_run_metrics import ScrapeRunMetrics
from app.models.scrape_run_alert import ScrapeRunAlert
from app.models.university_field_baseline import UniversityFieldBaseline

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global default floor thresholds (used when no baseline exists yet)
# ---------------------------------------------------------------------------

GLOBAL_DEFAULT_FLOORS: dict[str, float] = {
    # English — high priority
    "ielts_overall":     0.85,
    "toefl_overall":     0.70,
    "pte_overall":       0.70,
    "cambridge_overall": 0.60,
    "duolingo_overall":  0.60,
    # Fees
    "international_fee": 0.70,
    "domestic_fee":      0.50,
    "fee_term":          0.65,
    # Course details
    "duration":          0.90,
    "intake_months":     0.85,
    "degree_level":      0.90,
    "category":          0.80,
}

# ---------------------------------------------------------------------------
# Method-distribution quality rules
# ---------------------------------------------------------------------------

METHOD_QUALITY_RULES: list[dict[str, Any]] = [
    {
        "rule_id": "vision_dominance",
        "description": (
            "Vision OCR producing >40% of values for a field suggests "
            "page-text extractors are failing or vision is hallucinating"
        ),
        "fields": [
            "ielts_overall", "toefl_overall", "pte_overall",
            "duolingo_overall", "cambridge_overall",
        ],
        "method_prefix": "per_course_vision",
        "threshold": 0.40,
        "severity": "warning",
    },
    {
        "rule_id": "sibling_cache_dominance",
        "description": (
            "Sibling cache producing >30% of values means most values "
            "are propagated, not extracted per-course"
        ),
        "fields": "*",
        "method_prefix": "sibling_cache:",
        "threshold": 0.30,
        "severity": "warning",
    },
    {
        "rule_id": "ai_fallback_dominance",
        "description": (
            "AI fallback producing >25% of values means primary "
            "extractors are failing"
        ),
        "fields": "*",
        "method_prefix": "ai_fallback",
        "threshold": 0.25,
        "severity": "critical",
    },
    {
        "rule_id": "facebook_icon_source",
        "description": (
            "Evidence source URL contains 'facebook', 'instagram', or 'icon' — "
            "should never happen for English fields"
        ),
        "fields": [
            "ielts_overall", "toefl_overall", "pte_overall",
            "duolingo_overall", "cambridge_overall",
        ],
        "url_pattern": r"(facebook|instagram|icon[-_]|^link\.|logo)",
        "threshold": 0.0,
        "severity": "critical",
    },
]

_TREND_DROP_THRESHOLD = 0.15  # 15 percentage-point drop vs trailing mean fires a warning

# Week 2 P3 Rule 3 — URL-pattern anomaly thresholds.
# A URL prefix that has never appeared in the trailing N scrapes for this
# uni AND now contributes > _URL_PATTERN_MIN_STAGED stagings in this run
# is flagged as ``info`` so a human can confirm the new section is real
# (handbook archive, partner site) and not a discovery regression.
_URL_PATTERN_LOOKBACK_RUNS = 4
_URL_PATTERN_MIN_STAGED = 5
# Keep the prefix coarse so legitimate per-course URLs (which differ by
# slug) all collapse to the same prefix.  We use scheme://host/path[0:2]
# i.e. "https://uni.edu.au/courses/<category>" → "https://uni.edu.au/courses".
_URL_PATTERN_PATH_DEPTH = 2


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

@dataclass
class _MethodViolation:
    field: str
    method: str
    actual: float


def _method_matches(method: str, rule: dict[str, Any]) -> bool:
    prefix = rule.get("method_prefix", "")
    if not prefix:
        return False
    return method == prefix or method.startswith(prefix)


def _aggregate_by_field(metrics: list[ScrapeRunMetrics]) -> dict[str, float]:
    """Return total fill rate per field (summing across methods, capped at 1.0)."""
    totals: dict[str, int] = {}
    courses_total = metrics[0].courses_total if metrics else 1
    for m in metrics:
        totals[m.field_key] = totals.get(m.field_key, 0) + m.count
    return {
        fk: min(round(cnt / courses_total, 4), 1.0)
        for fk, cnt in totals.items()
    }


def _check_method_rule(
    metrics: list[ScrapeRunMetrics],
    rule: dict[str, Any],
    bad_url_counts: dict[str, int],
) -> list[_MethodViolation]:
    """Evaluate a single method-distribution or URL rule."""
    violations: list[_MethodViolation] = []

    field_universe = (
        list({m.field_key for m in metrics})
        if rule["fields"] == "*"
        else rule["fields"]
    )

    for fld in field_universe:
        field_metrics = [m for m in metrics if m.field_key == fld]
        if not field_metrics:
            continue
        total_count = sum(m.count for m in field_metrics)
        if total_count == 0:
            continue

        if "url_pattern" in rule:
            # URL-based rule: use pre-counted bad URL hits
            bad_count = bad_url_counts.get(fld, 0)
            rate = bad_count / total_count
        else:
            matching_count = sum(
                m.count for m in field_metrics if _method_matches(m.method, rule)
            )
            rate = matching_count / total_count

        if rate > rule["threshold"]:
            violations.append(_MethodViolation(
                field=fld,
                method=rule.get("method_prefix", rule.get("rule_id", "?")),
                actual=rate,
            ))

    return violations


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def evaluate_run_alerts(
    db: AsyncSession,
    scrape_run_id: str,
    university_id: int | None = None,
) -> list[ScrapeRunAlert]:
    """Check a completed scrape run against baselines + quality rules.

    Persists all generated alerts to ``scrape_run_alerts`` and returns them.
    Safe to call multiple times — existing rows for the run are deleted first.
    """
    # Load this run's metrics
    metrics_result = await db.execute(
        select(ScrapeRunMetrics).where(ScrapeRunMetrics.scrape_run_id == scrape_run_id)
    )
    metrics: list[ScrapeRunMetrics] = list(metrics_result.scalars().all())

    if not metrics:
        log.info("[ALERTS] run %s: no metrics rows — skipping alert evaluation", scrape_run_id)
        return []

    uni_id = university_id or metrics[0].university_id

    # Delete any stale rows from a previous attempt
    from sqlalchemy import delete as sa_delete
    await db.execute(
        sa_delete(ScrapeRunAlert).where(ScrapeRunAlert.scrape_run_id == scrape_run_id)
    )

    alerts: list[ScrapeRunAlert] = []

    # ── Load baselines for this university ─────────────────────────────────
    baselines_result = await db.execute(
        select(UniversityFieldBaseline).where(
            UniversityFieldBaseline.university_id == uni_id
        )
    )
    baselines: dict[str, UniversityFieldBaseline] = {
        b.field_key: b for b in baselines_result.scalars().all()
    }

    # ── Check 1: per-field fill-rate vs baseline floor ─────────────────────
    field_totals = _aggregate_by_field(metrics)
    for field_key, total_rate in field_totals.items():
        baseline = baselines.get(field_key)
        if baseline is not None:
            floor = float(baseline.floor_threshold)
        else:
            floor = GLOBAL_DEFAULT_FLOORS.get(field_key, 0.50)

        if total_rate < floor:
            gap = floor - total_rate
            severity = "critical" if gap >= 0.20 else "warning"
            alerts.append(ScrapeRunAlert(
                scrape_run_id=scrape_run_id,
                rule_id=f"fill_rate_drop:{field_key}",
                severity=severity,
                message=(
                    f"{field_key} fill-rate {total_rate:.0%} is below "
                    f"floor {floor:.0%} (gap={gap:.0%})"
                ),
                expected=floor,
                actual=total_rate,
            ))

    # ── Check 2: method-distribution rules ────────────────────────────────
    # Pre-fetch bad-URL counts for the url_pattern rules to avoid N+1 queries
    bad_url_counts: dict[str, int] = await _count_bad_url_evidence(db, scrape_run_id)

    for rule in METHOD_QUALITY_RULES:
        violations = _check_method_rule(metrics, rule, bad_url_counts)
        for v in violations:
            alerts.append(ScrapeRunAlert(
                scrape_run_id=scrape_run_id,
                rule_id=rule["rule_id"],
                severity=rule["severity"],
                message=(
                    f"{rule['description']} "
                    f"(field={v.field}, method={v.method}, rate={v.actual:.0%})"
                ),
                expected=rule.get("threshold"),
                actual=v.actual,
            ))

    # ── Check 2.5: URL-pattern anomaly (Week 2 P3 Rule 3) ─────────────────
    try:
        url_anomalies = await _detect_url_pattern_anomalies(db, scrape_run_id, uni_id)
        for prefix, staged_count in url_anomalies:
            alerts.append(ScrapeRunAlert(
                scrape_run_id=scrape_run_id,
                rule_id=f"url_pattern_anomaly:{prefix}",
                severity="info",
                message=(
                    f"new URL prefix {prefix!r} produced {staged_count} stagings "
                    f"this run but appeared 0 times in the last "
                    f"{_URL_PATTERN_LOOKBACK_RUNS} runs — confirm new section is "
                    f"intentional (handbook, partner site, blog)"
                ),
                expected=0,
                actual=staged_count,
            ))
    except Exception as exc:  # noqa: BLE001 — never block alert delivery on URL check
        log.warning("[ALERTS] url_pattern_anomaly check failed for run %s: %s", scrape_run_id, exc)

    # ── Check 3: trend detection vs last 5 runs ────────────────────────────
    recent_metrics = await _get_recent_run_metrics(db, uni_id, exclude_run=scrape_run_id, n=5)
    if len(recent_metrics) >= 3:
        # Compute trailing mean per field
        trailing: dict[str, list[float]] = {}
        for past_run in recent_metrics:
            ft = _aggregate_by_field(past_run)
            for fk, rate in ft.items():
                trailing.setdefault(fk, []).append(rate)

        for field_key, current_rate in field_totals.items():
            past_rates = trailing.get(field_key, [])
            if len(past_rates) < 3:
                continue
            trailing_mean = statistics.mean(past_rates)
            drop = trailing_mean - current_rate
            if drop >= _TREND_DROP_THRESHOLD:
                alerts.append(ScrapeRunAlert(
                    scrape_run_id=scrape_run_id,
                    rule_id=f"trend_drop:{field_key}",
                    severity="warning",
                    message=(
                        f"{field_key} dropped {drop:.0%} below "
                        f"trailing {len(past_rates)}-run average "
                        f"({trailing_mean:.0%} → {current_rate:.0%})"
                    ),
                    expected=trailing_mean,
                    actual=current_rate,
                ))

    # Persist all alerts
    if alerts:
        db.add_all(alerts)
        await db.commit()
        log.info(
            "[ALERTS] run %s: %d alerts (%d critical, %d warning)",
            scrape_run_id,
            len(alerts),
            sum(1 for a in alerts if a.severity == "critical"),
            sum(1 for a in alerts if a.severity == "warning"),
        )
    else:
        log.info("[ALERTS] run %s: no alerts fired", scrape_run_id)

    return alerts


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

async def _count_bad_url_evidence(
    db: AsyncSession, scrape_run_id: str
) -> dict[str, int]:
    """Count evidence rows whose source_url matches the facebook/icon pattern,
    broken down by field_key.  Returns {field_key: bad_count}."""
    from app.models.evidence import ScrapedFieldEvidence
    from app.models.scraped_course import ScrapedCourse
    from sqlalchemy import func

    _ICON_URL_RE = re.compile(
        r"(facebook|instagram|icon[-_]|logo)", re.IGNORECASE
    )

    # Pull all source URLs for selected evidence in this run (one DB round-trip)
    rows_result = await db.execute(
        select(
            ScrapedFieldEvidence.field_key,
            ScrapedFieldEvidence.source_url,
        )
        .join(ScrapedCourse, ScrapedCourse.id == ScrapedFieldEvidence.scraped_course_id)
        .where(ScrapedCourse.scrape_job_id == scrape_run_id)
        .where(ScrapedFieldEvidence.selected.is_(True))
        .where(ScrapedFieldEvidence.source_url.isnot(None))
    )
    counts: dict[str, int] = {}
    for row in rows_result.all():
        if _ICON_URL_RE.search(row.source_url or ""):
            counts[row.field_key] = counts.get(row.field_key, 0) + 1
    return counts


def _url_prefix(url: str, depth: int = _URL_PATTERN_PATH_DEPTH) -> str | None:
    """Coarse URL prefix: scheme://host/path[0:depth].  Returns None if unparseable."""
    if not url:
        return None
    from urllib.parse import urlparse
    try:
        p = urlparse(url)
    except Exception:  # noqa: BLE001
        return None
    if not p.scheme or not p.netloc:
        return None
    parts = [seg for seg in (p.path or "").split("/") if seg]
    if not parts:
        return f"{p.scheme}://{p.netloc}"
    return f"{p.scheme}://{p.netloc}/" + "/".join(parts[:depth])


async def _detect_url_pattern_anomalies(
    db: AsyncSession,
    scrape_run_id: str,
    university_id: int,
) -> list[tuple[str, int]]:
    """Return (prefix, staged_count) for URL prefixes new to this run.

    "New" = appeared zero times across this uni's last
    ``_URL_PATTERN_LOOKBACK_RUNS`` completed runs (excluding the current
    one) AND staged > ``_URL_PATTERN_MIN_STAGED`` rows in this run.
    """
    from app.models.scraped_course import ScrapedCourse
    from app.models.scrape_runtime import ScrapeRuntimeJob

    # Current-run staged URLs
    current_rows = await db.execute(
        select(ScrapedCourse.source_url)
        .where(ScrapedCourse.scrape_job_id == scrape_run_id)
        .where(ScrapedCourse.source_url.isnot(None))
    )
    current_counts: dict[str, int] = {}
    for (src_url,) in current_rows.all():
        pref = _url_prefix(src_url or "")
        if pref:
            current_counts[pref] = current_counts.get(pref, 0) + 1

    # Universe of "new" candidates by min-staged threshold
    candidates = {p for p, c in current_counts.items() if c > _URL_PATTERN_MIN_STAGED}
    if not candidates:
        return []

    # Trailing N completed runs for this uni (excluding current)
    job_ids_result = await db.execute(
        select(ScrapeRuntimeJob.runtime_job_id)
        .where(ScrapeRuntimeJob.university_id == university_id)
        .where(ScrapeRuntimeJob.status == "completed")
        .where(ScrapeRuntimeJob.runtime_job_id != scrape_run_id)
        .order_by(ScrapeRuntimeJob.completed_at.desc())
        .limit(_URL_PATTERN_LOOKBACK_RUNS)
    )
    historical_job_ids = [row[0] for row in job_ids_result.all()]
    if not historical_job_ids:
        # No history → suppress (every prefix would be "new" on first scrape)
        return []

    # Historical staged URLs
    hist_rows = await db.execute(
        select(ScrapedCourse.source_url)
        .where(ScrapedCourse.scrape_job_id.in_(historical_job_ids))
        .where(ScrapedCourse.source_url.isnot(None))
    )
    hist_prefixes: set[str] = set()
    for (src_url,) in hist_rows.all():
        pref = _url_prefix(src_url or "")
        if pref:
            hist_prefixes.add(pref)

    return sorted(
        ((p, current_counts[p]) for p in candidates if p not in hist_prefixes),
        key=lambda t: t[1],
        reverse=True,
    )


async def _get_recent_run_metrics(
    db: AsyncSession,
    university_id: int,
    exclude_run: str,
    n: int = 5,
) -> list[list[ScrapeRunMetrics]]:
    """Return per-run metric lists for the last ``n`` completed runs for
    this university (excluding the current run)."""
    from app.models.scrape_runtime import ScrapeRuntimeJob

    job_ids_result = await db.execute(
        select(ScrapeRuntimeJob.runtime_job_id)
        .where(ScrapeRuntimeJob.university_id == university_id)
        .where(ScrapeRuntimeJob.status == "completed")
        .where(ScrapeRuntimeJob.runtime_job_id != exclude_run)
        .order_by(ScrapeRuntimeJob.completed_at.desc())
        .limit(n)
    )
    job_ids = [row[0] for row in job_ids_result.all()]

    if not job_ids:
        return []

    groups: list[list[ScrapeRunMetrics]] = []
    for jid in job_ids:
        run_result = await db.execute(
            select(ScrapeRunMetrics).where(ScrapeRunMetrics.scrape_run_id == jid)
        )
        run_metrics = list(run_result.scalars().all())
        if run_metrics:
            groups.append(run_metrics)
    return groups
