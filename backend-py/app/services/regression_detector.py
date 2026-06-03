"""Regression detector — compares consecutive health snapshots and stores alerts.

Called by the daily health snapshot Celery task after snapshotting.
Can also be invoked manually via the API for on-demand detection.

Thresholds (all drops are compared as current < previous):
  overall_health     ≥ 15 pts → alert (≥30 pts = critical)
  discovery_health   ≥ 20 pts → alert (always high)
  extraction_health  ≥ 15 pts → alert (high if overall also dropped, else medium)
  fee_coverage       ≥ 20 pts → alert (always high)
  english_coverage   ≥ 20 pts → alert (always high)
  intake_coverage    ≥ 20 pts → alert (always high)
  course_count       > 20 %   → alert (>50% = critical, else high)

Guard: university must have ≥ 3 snapshots to trigger any alert.
Dedup: (university_id, alert_type, snapshot_date) UNIQUE where status != 'resolved'.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# ── Thresholds ─────────────────────────────────────────────────────────────────

THRESHOLDS: dict[str, dict[str, int]] = {
    "overall_health_drop":    {"pts": 15, "critical_pts": 30},
    "discovery_health_drop":  {"pts": 20},
    "extraction_health_drop": {"pts": 15},
    "fee_coverage_drop":      {"pts": 20},
    "english_coverage_drop":  {"pts": 20},
    "intake_coverage_drop":   {"pts": 20},
    "course_count_drop":      {"pct": 20, "critical_pct": 50},
}

PROBABLE_CAUSES: dict[str, list[str]] = {
    "course_count_drop": [
        "Discovery filter may be too restrictive — check allow_url_patterns in YAML",
        "University website structure changed — course listing URLs may have changed",
        "Anti-bot protection triggered — Cloudflare or similar may be blocking BFS",
        "Scrape was partial or hit page budget limit — check job logs for errors",
    ],
    "overall_health_drop": [
        "Multiple extraction fields failing — check Extraction tab for fill-rate pattern",
        "Recently changed website markup may have broken multiple extractors",
        "Completeness scores degraded across many courses",
    ],
    "discovery_health_drop": [
        "Course listing URL patterns changed on university website",
        "Sitemap unavailable, removed, or changed format",
        "BFS page budget exhausted before discovering all courses",
        "New anti-bot measures (Cloudflare, JS gating) blocking crawl",
    ],
    "extraction_health_drop": [
        "Course page template changed — extractors finding fewer fields",
        "Completeness scores dropped — check Extraction tab for lowest fill-rate fields",
        "Gemini/AI extraction may have been skipped due to cost ceiling or quota",
    ],
    "fee_coverage_drop": [
        "Fee table HTML structure changed on course pages",
        "PDF fee schedule not accessible or format changed",
        "Currency or fee format changed (e.g. AUD → $AUD, or range format)",
        "Central fee page URL changed — update uni YAML config",
    ],
    "english_coverage_drop": [
        "IELTS/PTE/TOEFL requirements section restructured on course pages",
        "Requirements moved to a separate PDF or page not being fetched",
        "English requirements section renamed or relocated",
    ],
    "intake_coverage_drop": [
        "Intake dates not yet published for the upcoming academic year",
        "Date format changed on course pages — extractor regex may need update",
        "Intake section removed, renamed, or moved to a different page",
    ],
}

ALERT_TYPE_LABELS: dict[str, str] = {
    "course_count_drop":     "Course Count Drop",
    "overall_health_drop":   "Overall Health Drop",
    "discovery_health_drop": "Discovery Health Drop",
    "extraction_health_drop":"Extraction Health Drop",
    "fee_coverage_drop":     "Fee Coverage Drop",
    "english_coverage_drop": "English Coverage Drop",
    "intake_coverage_drop":  "Intake Coverage Drop",
}


# ── Severity ───────────────────────────────────────────────────────────────────

def _severity(alert_type: str, delta_abs: float, overall_delta: float = 0) -> str:
    """Return 'critical', 'high', or 'medium' for the given alert."""
    if alert_type == "course_count_drop":
        return "critical" if delta_abs > 50 else "high"
    if alert_type == "overall_health_drop":
        return "critical" if delta_abs >= 30 else "high"
    if alert_type in ("discovery_health_drop", "fee_coverage_drop",
                      "english_coverage_drop", "intake_coverage_drop"):
        return "high"
    if alert_type == "extraction_health_drop":
        return "high" if overall_delta >= 15 else "medium"
    return "medium"


# ── Core detection (pure, no DB) ───────────────────────────────────────────────

def detect_regressions(
    current: dict[str, Any],
    previous: dict[str, Any],
    history_count: int,
    overall_current_delta: float = 0,
) -> list[dict[str, Any]]:
    """Compare two snapshot rows and return a list of alert dicts (no DB writes).

    Returns an empty list if history_count < 3 (guard for brand-new universities).
    """
    if history_count < 3:
        return []

    alerts: list[dict[str, Any]] = []

    def _alert(alert_type: str, prev_val: float, cur_val: float, delta_abs: float, sev: str) -> None:
        alerts.append({
            "alert_type":      alert_type,
            "severity":        sev,
            "previous_value":  prev_val,
            "current_value":   cur_val,
            "delta":           -(delta_abs),  # negative = drop
            "probable_causes": PROBABLE_CAUSES.get(alert_type, []),
        })

    # ── Course count drop ──────────────────────────────────────────────────────
    prev_courses = previous.get("total_courses") or 0
    cur_courses  = current.get("total_courses")  or 0
    if prev_courses > 0:
        drop_pct = 100.0 * (prev_courses - cur_courses) / prev_courses
        if drop_pct > THRESHOLDS["course_count_drop"]["pct"]:
            _alert("course_count_drop", prev_courses, cur_courses, drop_pct,
                   _severity("course_count_drop", drop_pct))

    # ── Point-based metric drops ───────────────────────────────────────────────
    point_checks: list[tuple[str, str, str]] = [
        ("overall_health_drop",    "overall_health",    "overall_health_drop"),
        ("discovery_health_drop",  "discovery_health",  "discovery_health_drop"),
        ("extraction_health_drop", "extraction_health", "extraction_health_drop"),
        ("fee_coverage_drop",      "fee_coverage",      "fee_coverage_drop"),
        ("english_coverage_drop",  "english_coverage",  "english_coverage_drop"),
        ("intake_coverage_drop",   "intake_coverage",   "intake_coverage_drop"),
    ]
    overall_delta_abs = max(0.0, float(previous.get("overall_health") or 0) - float(current.get("overall_health") or 0))

    for _, field, alert_type in point_checks:
        prev_val = float(previous.get(field) or 0)
        cur_val  = float(current.get(field)  or 0)
        delta    = prev_val - cur_val
        threshold = THRESHOLDS[alert_type].get("pts", 999)
        if delta >= threshold:
            sev = _severity(alert_type, delta, overall_delta_abs)
            _alert(alert_type, prev_val, cur_val, delta, sev)

    return alerts


# ── DB-backed detection ────────────────────────────────────────────────────────

async def run_regression_detection(db: AsyncSession) -> dict[str, int]:
    """Detect regressions for all universities with ≥3 snapshots and store alerts.

    Returns a summary dict: {"universities_checked": N, "alerts_created": M}.
    """
    # Fetch all universities with ≥3 snapshots, their two most recent snapshots,
    # plus the total count of snapshots per university.
    rows = await db.execute(text("""
        WITH ranked AS (
            SELECT
                university_id,
                snapshot_date,
                overall_health,
                discovery_health,
                extraction_health,
                fee_coverage,
                english_coverage,
                intake_coverage,
                total_courses,
                ROW_NUMBER() OVER (PARTITION BY university_id ORDER BY snapshot_date DESC) AS rn
            FROM university_health_snapshots
        ),
        counts AS (
            SELECT university_id, COUNT(*) AS total_snaps
            FROM university_health_snapshots
            GROUP BY university_id
        )
        SELECT
            r.university_id,
            r.snapshot_date,
            r.overall_health,
            r.discovery_health,
            r.extraction_health,
            r.fee_coverage,
            r.english_coverage,
            r.intake_coverage,
            r.total_courses,
            r.rn,
            c.total_snaps
        FROM ranked r
        JOIN counts c USING (university_id)
        WHERE c.total_snaps >= 3
          AND r.rn <= 2
        ORDER BY r.university_id, r.rn
    """))

    # Group into pairs: {uni_id: {1: current_row, 2: previous_row}}
    pairs: dict[int, dict[int, dict]] = {}
    for row in rows.mappings():
        uid  = row["university_id"]
        rn   = row["rn"]
        snaps = row["total_snaps"]
        if uid not in pairs:
            pairs[uid] = {"count": snaps}
        pairs[uid][rn] = dict(row)

    unis_checked = 0
    alerts_created = 0
    alert_university_ids: set[int] = set()

    for uid, data in pairs.items():
        if 1 not in data or 2 not in data:
            continue  # Need both current and previous

        current  = data[1]
        previous = data[2]
        count    = data["count"]
        snapshot_date = str(current["snapshot_date"])

        overall_delta = max(0.0, float(previous["overall_health"] or 0) - float(current["overall_health"] or 0))
        new_alerts = detect_regressions(current, previous, count, overall_delta)
        unis_checked += 1

        for alert in new_alerts:
            # Upsert-style: skip if same (uni, type, date) already non-resolved
            existing = await db.execute(text("""
                SELECT id FROM university_regression_alerts
                WHERE university_id = :uid
                  AND alert_type    = :atype
                  AND snapshot_date = :sdate
                  AND status       != 'resolved'
                LIMIT 1
            """), {"uid": uid, "atype": alert["alert_type"], "sdate": snapshot_date})
            if existing.scalar():
                continue  # Duplicate — skip

            await db.execute(text("""
                INSERT INTO university_regression_alerts
                    (university_id, alert_type, severity, previous_value,
                     current_value, delta, probable_causes, status, snapshot_date)
                VALUES
                    (:uid, :atype, :sev, :prev, :cur, :delta, :causes::jsonb, 'open', :sdate)
            """), {
                "uid":    uid,
                "atype":  alert["alert_type"],
                "sev":    alert["severity"],
                "prev":   alert["previous_value"],
                "cur":    alert["current_value"],
                "delta":  alert["delta"],
                "causes": json.dumps(alert["probable_causes"]),
                "sdate":  snapshot_date,
            })
            alerts_created += 1
            alert_university_ids.add(uid)

    if alerts_created > 0:
        await db.commit()

    log.info("regression_detection: checked=%d alerts_created=%d", unis_checked, alerts_created)
    return {
        "universities_checked": unis_checked,
        "alerts_created":       alerts_created,
        "affected_university_ids": list(alert_university_ids),
    }
