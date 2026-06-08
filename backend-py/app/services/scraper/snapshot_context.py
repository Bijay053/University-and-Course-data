"""ContextVars for snapshot metadata during a live scrape job.

Set at the start of run_scrape() so all fetch helpers can save snapshots
without needing university_id / scrape_job_id threaded through every call.

Pending-snapshot staging
------------------------
http_fetcher calls ``stage_snapshot()`` each time it successfully fetches a
course page.  Because retries overwrite the same ContextVar slot, only the
*last* successful fetch within each course-extraction task is kept — which is
exactly the HTML that extract_course() finally operated on.

After extract_course() returns, _extract_only() calls
``consume_pending_snapshot()`` to read + clear the slot, then fires the S3
save (with the extraction result attached as ``original_extraction``).

This design means:
  • Retries do NOT produce multiple snapshots — only the winning fetch is saved.
  • Discovery / central-page fetches are never saved (no active scrape scope).
  • Snapshot saves never block the live scrape (fire-and-forget in orchestrator).
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Generator

_snapshot_university_id: ContextVar[int | None] = ContextVar(
    "_snapshot_university_id", default=None
)
_snapshot_job_id: ContextVar[str | None] = ContextVar(
    "_snapshot_job_id", default=None
)
# Set True only during replay — suppresses live fetch, reads S3 instead
_snapshot_replay_mode: ContextVar[bool] = ContextVar(
    "_snapshot_replay_mode", default=False
)

# Per-task slot for the *last* HTML fetched for the current course URL.
# Overwritten on each retry; only the final successful fetch is saved.
_pending_snapshot: ContextVar[dict[str, Any] | None] = ContextVar(
    "_pending_snapshot", default=None
)


@contextmanager
def snapshot_job_scope(
    university_id: int, scrape_job_id: str
) -> "Generator[None, None, None]":
    """Set snapshot context for the duration of one scrape job."""
    t1 = _snapshot_university_id.set(university_id)
    t2 = _snapshot_job_id.set(scrape_job_id)
    try:
        yield
    finally:
        _snapshot_university_id.reset(t1)
        _snapshot_job_id.reset(t2)


def get_snapshot_context() -> tuple[int | None, str | None]:
    """Return (university_id, scrape_job_id) from current context, or (None, None)."""
    return _snapshot_university_id.get(), _snapshot_job_id.get()


def is_replay_mode() -> bool:
    return _snapshot_replay_mode.get()


@contextmanager
def replay_mode_scope() -> "Generator[None, None, None]":
    token = _snapshot_replay_mode.set(True)
    try:
        yield
    finally:
        _snapshot_replay_mode.reset(token)


def stage_snapshot(url: str, content: str | bytes, fetch_method: str) -> None:
    """Store the latest successfully fetched content for this course-extraction task.

    Called by http_fetcher after each successful fetch.  Because this is a
    ContextVar, it is task-local — concurrent course extractions don't
    interfere with each other.  Retries simply overwrite the previous value,
    so only the final fetch is retained.
    """
    if is_replay_mode():
        return  # never stage during replay
    uni_id, job_id = get_snapshot_context()
    if not uni_id or not job_id:
        return  # no active scrape scope (e.g. discovery / central pages)
    _pending_snapshot.set({
        "url": url,
        "content": content,
        "fetch_method": fetch_method,
    })


def consume_pending_snapshot() -> dict[str, Any] | None:
    """Read and clear the pending snapshot for this task.

    Returns a dict with keys ``url``, ``content``, ``fetch_method``,
    or None if no snapshot was staged.
    """
    v = _pending_snapshot.get()
    _pending_snapshot.set(None)
    return v
