"""Contextvar for the current per-university scraper configuration.

The contextvar is set once at the start of every ``run_scrape`` call and
is available to all coroutines on the same asyncio task-tree for the
duration of that job.

Design rationale
----------------
Celery uses prefork workers.  Each worker process handles one scrape job at a
time, so there is no cross-job contamination at the process level.  Within a
single job, all asyncio coroutines share the same event loop and therefore the
same ContextVar value set by ``run_scrape``.

Context inheritance for asyncio.gather()
-----------------------------------------
Tasks created inside ``asyncio.gather()`` inherit the context of the creating
task (Python 3.7+ behaviour).  Since all course-extraction coroutines are
launched from within the same ``run_scrape`` / ``run_repair`` call that set
the contextvar, all coroutines automatically see the correct ``UniConfig``
without any additional wiring.

There is no per-thread state issue: all I/O-bound extraction is async, never
spawned into a thread pool that would not inherit the contextvar.

For tests: call ``set_uni_config(mock_config)`` before the function under
test.  No fixtures required.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Optional

from app.services.scraper.config.schema import UniConfig

log = logging.getLogger(__name__)

current_uni_config: ContextVar[Optional[UniConfig]] = ContextVar(
    "current_uni_config", default=None
)


def get_uni_config() -> Optional[UniConfig]:
    """Return the UniConfig for the running scrape job, or None if not set.

    Code that is not yet migrated to config-driven behaviour should ignore
    a None return and fall back to the existing hardcoded logic unchanged.
    """
    return current_uni_config.get()


def set_uni_config(config: UniConfig) -> None:
    """Set the UniConfig for the running scrape job.

    Called once per job in ``orchestrator.run_scrape`` and ``repair.run_repair``
    immediately after the University row is loaded from the database.
    """
    current_uni_config.set(config)


def require_uni_config() -> UniConfig:
    """Return the current UniConfig, or fall back to bare defaults with a warning.

    Use this in extractor functions that will be migrated to config-driven
    behaviour in Week 2+.  The soft-fail design means production never crashes
    due to a missing ``set_uni_config()`` call at an entry point.

    During Weeks 2–4, watch the scraper logs for::

        WARNING extractor called without uni context

    Any such log line means a code path is bypassing the normal entry points
    (``run_scrape`` / ``run_repair``) without calling ``set_uni_config()``.
    File a bug and add the call.

    Once all entry points are confirmed wired, this function can be updated
    to raise instead of warn — but do not make that change while any
    migrations are still in progress.
    """
    cfg = current_uni_config.get()
    if cfg is None:
        log.warning(
            "extractor called without uni context — set_uni_config() was not "
            "called at the entry point (repair job? test fixture? direct CLI call?). "
            "Falling back to bare defaults. "
            "See backend-py/app/services/scraper/config/context.py for remediation."
        )
        return UniConfig(
            slug="__no_context__",
            name="unknown — no context",
            base_url="",
            scrape_url="",
        )
    return cfg
