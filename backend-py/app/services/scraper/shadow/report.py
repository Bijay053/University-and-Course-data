"""Write shadow-mode diff reports to shadow_reports/ as JSON files.

File naming:  shadow_reports/{timestamp}_{slug}_{uni_id}_run{N}.json

Each file records:
  - run metadata (timestamp, uni_id, slug, run_number, clean streak so far)
  - the DiffReport as a structured dict
  - a human-readable summary line

The run_number and clean_streak are derived from existing files in the
shadow_reports/ directory so callers don't need to track state manually.

Cutover criterion: 5 consecutive clean runs (each from a fresh scrape
scheduled ≥ 1 hour apart).  The streak counter resets on any dirty run.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.scraper.shadow.diff import DiffReport

log = logging.getLogger(__name__)

SHADOW_REPORTS_DIR = Path(__file__).resolve().parents[4] / "shadow_reports"
CUTOVER_STREAK_NEEDED = 5


def _report_dir() -> Path:
    SHADOW_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    return SHADOW_REPORTS_DIR


def _existing_reports(uni_id: int, slug: str) -> list[Path]:
    """Return all existing reports for this uni, sorted oldest-first."""
    pattern = re.compile(rf"^\d{{14}}_{re.escape(slug)}_{uni_id}_run\d+\.json$")
    reports = [
        p
        for p in _report_dir().iterdir()
        if p.is_file() and pattern.match(p.name)
    ]
    return sorted(reports, key=lambda p: p.name)


def _current_streak(reports: list[Path]) -> int:
    """Return current consecutive clean-run streak from the most recent reports."""
    streak = 0
    for path in reversed(reports):
        try:
            data = json.loads(path.read_text())
            if data.get("is_clean"):
                streak += 1
            else:
                break
        except Exception:
            break
    return streak


def write_shadow_report(
    diff: DiffReport,
    *,
    uni_id: int,
    slug: str,
    old_job_id: str = "",
    new_job_id: str = "",
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write a shadow diff report and return the path to the written file.

    Also logs a summary line at INFO level so Celery workers emit it to
    their standard log stream.

    Args:
        diff:       DiffReport from diff_staged_runs()
        uni_id:     University ID (used in filename and metadata)
        slug:       University slug (used in filename)
        old_job_id: Scrape job ID for the old path (for traceability)
        new_job_id: Scrape job ID for the new path
        extra:      Any additional metadata to embed in the report

    Returns:
        Path to the written JSON file.
    """
    existing = _existing_reports(uni_id, slug)
    run_number = len(existing) + 1
    streak_before = _current_streak(existing)
    new_streak = (streak_before + 1) if diff.is_clean else 0

    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    filename = f"{ts}_{slug}_{uni_id}_run{run_number:03d}.json"
    path = _report_dir() / filename

    report_data: dict[str, Any] = {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uni_id": uni_id,
        "slug": slug,
        "run_number": run_number,
        "old_job_id": old_job_id,
        "new_job_id": new_job_id,
        "is_clean": diff.is_clean,
        "summary": diff.summary,
        "clean_streak": new_streak,
        "cutover_threshold": CUTOVER_STREAK_NEEDED,
        "cutover_ready": new_streak >= CUTOVER_STREAK_NEEDED,
        "diff": diff.as_dict(),
    }
    if extra:
        report_data["extra"] = extra

    path.write_text(json.dumps(report_data, indent=2, default=str))

    streak_msg = (
        f"streak={new_streak}/{CUTOVER_STREAK_NEEDED}"
        + (" — READY FOR CUTOVER" if report_data["cutover_ready"] else "")
    )
    log.info(
        "shadow[%s/%d] run=%d %s | %s",
        slug,
        uni_id,
        run_number,
        diff.summary,
        streak_msg,
    )

    return path
