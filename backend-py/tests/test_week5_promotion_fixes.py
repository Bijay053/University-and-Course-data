"""Week 5 — regression tests for the Charles Sturt promotion-gap fixes.

Covers two bugs:

1. ``approve_scraped_course`` crashed with AttributeError on ``None.lower()``
   when ``course_name`` was NULL/empty.  Should now raise a clear ValueError
   BEFORE opening the SQLAlchemy transaction.

2. ``bulk_approve.py``'s per-row except block did NOT call ``db.rollback()``,
   so one bad row poisoned the session and made every subsequent row fail
   with "transaction has been rolled back".  Should now rollback per-row.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.scraper.approve_course import approve_scraped_course


@pytest.mark.asyncio
async def test_approve_raises_valueerror_on_null_course_name():
    """Bug 1: empty/NULL course_name must raise ValueError before any DB I/O."""
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    sc = SimpleNamespace(id=42, course_name=None, university_id=1)

    with pytest.raises(ValueError, match=r"id=42.*empty course_name"):
        await approve_scraped_course(db, sc)

    # Critical: no DB I/O happened — the guard must be BEFORE the transaction.
    db.execute.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_approve_raises_valueerror_on_whitespace_course_name():
    """Bug 1 variant: whitespace-only name is also rejected."""
    db = MagicMock()
    db.execute = AsyncMock()
    sc = SimpleNamespace(id=99, course_name="   \n\t  ", university_id=1)

    with pytest.raises(ValueError, match=r"id=99"):
        await approve_scraped_course(db, sc)
    db.execute.assert_not_called()


def test_bulk_approve_calls_rollback_on_per_row_exception():
    """Bug 2: bulk_approve.py must call ``db.rollback()`` in its except block.

    Source-level inspection — the runtime path requires a real DB so we
    assert on the source instead.  This guards against a future refactor
    silently removing the rollback (which is exactly what shipped the
    Charles Sturt 92-course gap to prod).
    """
    import inspect, re
    from scripts import bulk_approve

    src = inspect.getsource(bulk_approve.run)
    # except block must be followed by an `await db.rollback()` before
    # the failure is logged.
    pattern = re.compile(
        r"except\s+Exception[^:]*:\s*"
        r"(?:#[^\n]*\n\s*)*"          # optional comment lines
        r"await\s+db\.rollback\(\)",
        re.MULTILINE,
    )
    assert pattern.search(src), (
        "bulk_approve.run() lost its db.rollback() call in the per-row "
        "except block. This was the root cause of the Charles Sturt "
        "92-course promotion gap — without rollback, one bad row poisons "
        "the SQLAlchemy session and every subsequent row in the batch "
        "fails with 'transaction has been rolled back'."
    )
