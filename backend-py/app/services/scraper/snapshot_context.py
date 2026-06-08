"""ContextVars for snapshot metadata during a live scrape job.

Set at the start of run_scrape() so all fetch helpers can save snapshots
without needing university_id / scrape_job_id threaded through every call.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Generator

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
