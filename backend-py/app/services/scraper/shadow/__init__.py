"""Shadow-mode scaffolding for per-uni extraction path migration.

Shadow mode runs both the old and new extraction code paths in parallel during
a single scrape job, diffs their outputs, and writes a structured report.
Only the old path result is staged to scraped_courses — shadow mode is
verification only, not deployment.

Enable per env var:
    SHADOW_MODE_UNI_IDS=41           # ACAP only
    SHADOW_MODE_UNI_IDS=41,20,87     # multiple unis
    SHADOW_MODE_UNI_IDS=*            # all unis

Cutover (new path becomes authoritative):
    SHADOW_CUTOVER_UNI_IDS=41        # ACAP has 5-run clean streak

See diff.py for normalization rules and DiffReport.
See report.py for JSON report format and shadow_reports/ layout.
See new_path.py for the new extraction code path injection point.
"""

from app.services.scraper.shadow.diff import DiffReport, diff_staged_runs
from app.services.scraper.shadow.mode import is_shadow_enabled, is_cutover
from app.services.scraper.shadow.report import write_shadow_report

__all__ = [
    "DiffReport",
    "diff_staged_runs",
    "is_shadow_enabled",
    "is_cutover",
    "write_shadow_report",
]
