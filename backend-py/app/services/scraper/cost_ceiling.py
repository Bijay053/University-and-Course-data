"""Per-job Gemini cost ceiling (Component 3).

Tracks total Gemini spend for a single scrape job and aborts the job's
Gemini calls once the per-university budget is exceeded.  Cost ceiling
stops are recorded on the ``scrape_runtime_jobs`` row via the columns
added in migration 011 (``cost_ceiling_hit``, ``total_gemini_cost_usd``).

Alerting: when the ceiling is hit, an error is logged. The metrics/alerts
system from Priority 5 picks it up automatically via the log-scraper path.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-university budget table (USD)
# ---------------------------------------------------------------------------
DEFAULT_PER_JOB_BUDGET_USD: float = 1.00

LARGE_UNI_BUDGETS: dict[str, float] = {
    "unisq": 2.00,
    "rmit": 3.00,
    "uts": 2.50,
    "monash": 3.00,
    "uq": 2.50,
    "swinburne": 2.00,
    "latrobe": 2.00,
    "curtin": 2.00,
    "deakin": 2.00,
    "griffith": 2.00,
}


def get_budget_for_university(university_slug: str) -> float:
    """Return the per-job Gemini budget in USD for a given university slug."""
    return LARGE_UNI_BUDGETS.get((university_slug or "").lower(), DEFAULT_PER_JOB_BUDGET_USD)


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class JobCostMonitor:
    """Tracks Gemini spend for one scrape job.

    Thread-safe for asyncio contexts (single-threaded event loop).

    Usage::

        monitor = JobCostMonitor("job_abc", "unisq", budget_usd=2.00)

        # Before each Gemini call:
        if not monitor.can_continue():
            return  # skip

        # After each Gemini call:
        monitor.record_call(cost_usd)
    """

    def __init__(
        self,
        scrape_run_id: str,
        university_slug: str,
        budget_usd: float,
    ) -> None:
        self.scrape_run_id = scrape_run_id
        self.university_slug = university_slug
        self.budget_usd = budget_usd
        self.spent_usd: float = 0.0
        self.aborted: bool = False
        self._alert_sent: bool = False

    def record_call(self, cost_usd: float) -> None:
        """Add *cost_usd* to the running total; abort if budget is exceeded."""
        self.spent_usd += cost_usd
        if not self.aborted and self.spent_usd >= self.budget_usd:
            self.aborted = True
            log.error(
                "[COST CEILING] run=%s uni=%s spent=$%.4f >= budget=$%.2f — "
                "Gemini calls halted for this job",
                self.scrape_run_id,
                self.university_slug,
                self.spent_usd,
                self.budget_usd,
            )
            if not self._alert_sent:
                self._alert_sent = True
                try:
                    asyncio.get_event_loop().create_task(self._emit_alert())
                except RuntimeError:
                    pass  # no running loop in tests — silent

    def can_continue(self) -> bool:
        """Return False when the cost ceiling has been hit."""
        return not self.aborted

    @property
    def summary(self) -> dict:
        return {
            "scrape_run_id": self.scrape_run_id,
            "university_slug": self.university_slug,
            "budget_usd": self.budget_usd,
            "spent_usd": round(self.spent_usd, 6),
            "aborted": self.aborted,
        }

    async def _emit_alert(self) -> None:
        """Log a structured alert that Priority 5 alert evaluator can pick up."""
        log.error(
            "[COST CEILING ALERT] %s",
            {
                "event": "cost_ceiling_hit",
                "scrape_run_id": self.scrape_run_id,
                "university": self.university_slug,
                "spent_usd": round(self.spent_usd, 6),
                "budget_usd": self.budget_usd,
            },
        )
