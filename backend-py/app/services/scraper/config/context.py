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

For tests: call ``set_uni_config(mock_config)`` before the function under
test.  No fixtures required.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

from app.services.scraper.config.schema import UniConfig

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

    Called once per job in ``orchestrator.run_scrape`` immediately after the
    University row is loaded from the database.
    """
    current_uni_config.set(config)
