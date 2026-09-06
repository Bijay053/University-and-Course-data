"""Regression coverage for in-flight course extraction progress."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.services.scraper import orchestrator


class _SessionContext:
    def __init__(self, session: AsyncMock) -> None:
        self.session = session

    async def __aenter__(self) -> AsyncMock:
        return self.session

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_runtime_progress_is_persisted_before_terminal_finalization(monkeypatch):
    """A running job gets a durable current value after one course completes."""
    session = AsyncMock()
    monkeypatch.setattr(
        orchestrator,
        "AsyncSessionLocal",
        lambda: _SessionContext(session),
    )

    await orchestrator._persist_runtime_progress("job_in_flight", 3)

    statement, params = session.execute.await_args.args
    sql = str(statement)
    assert "SET current = :current" in sql
    assert "status IN ('running', 'awaiting_approval')" in sql
    assert params == {"current": 3, "job_id": "job_in_flight"}
    session.commit.assert_awaited_once()


def test_completion_progress_is_retry_safe_and_resume_aware():
    """Retries only advance after settling; resumed work starts at its offset."""
    source = Path(orchestrator.__file__).read_text(encoding="utf-8")
    resume_default = source.index("_resume_already_staged = 0")
    resume_filter = source.index("_already_staged_checkpoint_rows(", resume_default)
    completion_seed = source.index("completed = [_resume_already_staged]", resume_filter)
    first_batch = source.index("for _batch_idx, _batch_links", completion_seed)
    assert resume_default < resume_filter < completion_seed < first_batch

    retry_branch = source.index("result.get(\"_retry_after\")")
    retry_continue = source.index("continue", retry_branch)
    completion_call = source.index(
        "await _record_extraction_complete(link)",
        retry_continue,
    )
    assert retry_branch < retry_continue < completion_call