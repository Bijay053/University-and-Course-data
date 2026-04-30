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

Thread / executor safety
-------------------------
The scraper code that reaches an extractor is entirely async (no
``run_in_executor``, ``ThreadPoolExecutor``, ``ProcessPoolExecutor``, or
``concurrent.futures`` calls).  The one ``asyncio.to_thread()`` site is in
``pdf_vision._render_pdf_to_jpegs`` — that function renders PDF pages to JPEG
images; it never calls an extractor or reads the contextvar.
``asyncio.to_thread`` copies the context anyway (Python 3.9+), so even if a
future extractor were added there it would Just Work.

If any future code spawns work via ``loop.run_in_executor()`` or a raw thread
pool, verify explicitly that it does not call an extractor.  If it does,
capture the UniConfig before the ``submit()`` call and pass it as a regular
argument — do **not** rely on contextvar inheritance across a raw thread
boundary.

Guard mode: STRICT vs SOFT
----------------------------
``require_uni_config()`` behaviour is controlled by the
``UNI_CONFIG_GUARD_MODE`` environment variable:

* ``strict`` *(default)* — raises ``RuntimeError`` immediately when the
  contextvar is unset.  Use this during Weeks 2–4 while extractor migration
  is ongoing.  A hard crash makes a missing ``set_uni_config()`` call
  impossible to miss.

* ``soft`` — logs a WARNING and returns bare defaults.  Intended for
  post-Week-4 production once all entry points have been confirmed wired and
  tested.  Switch by setting ``UNI_CONFIG_GUARD_MODE=soft`` on the server.

Default is ``strict``.  Override in production *after* Week 4 stabilisation.

For tests: call ``set_uni_config(mock_config)`` before the function under
test.  No fixtures required.
"""
from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from typing import Optional

from app.services.scraper.config.schema import UniConfig

log = logging.getLogger(__name__)

current_uni_config: ContextVar[Optional[UniConfig]] = ContextVar(
    "current_uni_config", default=None
)

# Read once at import time so the mode is stable for the process lifetime.
# Celery workers each import this module in their own process, so setting
# UNI_CONFIG_GUARD_MODE on the environment before worker start is sufficient.
_GUARD_MODE: str = os.environ.get("UNI_CONFIG_GUARD_MODE", "strict").lower().strip()


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
    """Return the current UniConfig, raising or warning when it is unset.

    Behaviour depends on ``UNI_CONFIG_GUARD_MODE`` (default: ``strict``):

    **strict** — raises ``RuntimeError``.  The job crashes loudly, the missing
    ``set_uni_config()`` call is found and fixed immediately.  Use during
    Weeks 2–4 while extractor migration is in progress.

    **soft** — logs a WARNING and returns bare defaults so the job continues.
    Use in production *after* Week 4 once all entry points are confirmed wired.
    Set ``UNI_CONFIG_GUARD_MODE=soft`` on the server to enable.

    A ``"extractor called without uni context"`` log line in soft mode means
    a code path is bypassing the normal entry points (``run_scrape`` /
    ``run_repair``) without calling ``set_uni_config()``.  File a bug.
    """
    cfg = current_uni_config.get()
    if cfg is not None:
        return cfg

    msg = (
        "extractor called without uni context — set_uni_config() was not "
        "called at the entry point (new background task? test fixture? "
        "direct CLI call?).  "
        "See backend-py/app/services/scraper/config/context.py for remediation."
    )

    if _GUARD_MODE != "soft":
        raise RuntimeError(
            msg + "  (UNI_CONFIG_GUARD_MODE=strict — set to 'soft' to demote to a warning)"
        )

    log.warning(msg + "  Falling back to bare defaults.")
    return UniConfig(
        slug="__no_context__",
        name="unknown — no context",
        base_url="",
        scrape_url="",
    )
