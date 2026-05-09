"""Week 2 P6 — sanity floors with log-and-accept semantics.

Historically, validation rejected (set to None) any value below a
"reasonable" floor:

  * IELTS overall  < 5.0
  * Annual fee     < $5_000
  * Duration       < 0.25 years

Those floors were calibrated for typical undergraduate courses and
wrongly dropped:

  * Pathway / ELICOS programs (IELTS 4.5)
  * TAFE diplomas + short courses (< $5K/year)
  * Micro-credentials (3-week = 0.06 years)

Fix: lower the floors AND change the semantics from "reject" to
"log and accept".  Low values are real for some course types; suppressing
them silently turns a parse problem into a data-loss problem.

Usage:

    from app.services.scraper.sanity_floors import sanity_check
    fee = sanity_check("international_fee_annual", fee)  # always returns the value

Callers may inspect the global counters via ``get_sanity_counters()``
for inclusion in the run summary.
"""
from __future__ import annotations

import logging
from threading import Lock

log = logging.getLogger(__name__)

# Each entry is the *historic* hard-reject threshold.  Values strictly
# below the floor are now ACCEPTED (the new behaviour) but logged + counted
# so reviewers can audit the grey zone.  Values that are clearly noise
# (well below the floor) are still rejected by callers via a separate
# hard floor (e.g. 1_000 for fees in extractors/fee.py); this module is
# only responsible for the log-and-accept window above that hard floor.
SANITY_FLOORS: dict[str, float] = {
    "ielts_overall":            5.0,    # historic reject; pathways allow 4.0–4.5
    "international_fee_annual": 5_000,  # historic reject; TAFE/short-course can be < 5K
    "international_fee":        5_000,  # alias used by extractors/fee.py
    "domestic_fee":             5_000,
    "duration_years":           0.25,   # historic reject; 2-3 week micro-credentials < 0.25
    "duration":                 0.25,   # alias
}

# Process-local counters for diagnostics.  Reset by callers between runs
# if granular accounting per scrape is needed.
_counters: dict[str, int] = {}
_counters_lock = Lock()


def sanity_check(field: str, value: float | int | None) -> float | int | None:
    """Return ``value`` unchanged.  If it is below the configured floor,
    log a SANITY note and increment a per-field counter."""
    if value is None:
        return value
    floor = SANITY_FLOORS.get(field)
    if floor is None:
        return value
    try:
        if float(value) < float(floor):
            log.info(
                "[SANITY LOG] %s=%s below floor %s — accepting anyway "
                "(low values are valid for pathway/short-course/micro-credential)",
                field, value, floor,
            )
            with _counters_lock:
                _counters[field] = _counters.get(field, 0) + 1
    except (TypeError, ValueError):
        pass
    return value


def get_sanity_counters() -> dict[str, int]:
    """Return a snapshot of the below-floor counters."""
    with _counters_lock:
        return dict(_counters)


def reset_sanity_counters() -> None:
    with _counters_lock:
        _counters.clear()
