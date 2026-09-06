"""Regression coverage for in-flight course extraction progress."""

from __future__ import annotations

import asyncio
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


@pytest.mark.asyncio
async def test_resumed_mixed_outcomes_advance_once_after_settlement():
    """The production retry loop keeps durable and emitted progress reconciled."""
    links = [
        {"name": "Success", "url": "https://example.test/success"},
        {"name": "Circuit-breaker skip", "url": "https://example.test/skipped"},
        {"name": "Timed out", "url": "https://example.test/timeout"},
        {"name": "Exception", "url": "https://example.test/exception"},
        {"name": "Cooldown", "url": "https://example.test/cooldown"},
    ]
    outcomes = {
        "Success": [{"name": "Success"}],
        "Circuit-breaker skip": [{
            "name": "Circuit-breaker skip",
            "error": "extract: Scrape.do auth/credits error HTTP 401",
        }],
        "Timed out": [{"name": "Timed out", "error": "per_course_timeout", "_timed_out": True}],
        "Exception": [RuntimeError("extract failed")],
        "Cooldown": [
            {"name": "Cooldown", "_retry_after": 0.01, "error": "rate_limited"},
            {"name": "Cooldown"},
        ],
    }
    attempts: list[str] = []
    persisted: list[tuple[int, int]] = []
    emitted: list[dict] = []
    completed = [7]  # resumed checkpoint offset
    total = 12
    lock = asyncio.Lock()

    async def record_complete(link: dict) -> None:
        async with lock:
            completed[0] += 1
            payload = {"current": completed[0], "total": total, "url": link["url"]}
            persisted.append((payload["current"], payload["total"]))
            emitted.append(payload)

    async def run(link: dict):
        async def attempt():
            attempts.append(link["name"])
            outcome = outcomes[link["name"]].pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return await orchestrator._settle_course_with_retries(
            link, attempt, record_complete, sleep=AsyncMock()
        )

    results = await asyncio.gather(*(run(link) for link in links))

    assert completed == [12]
    assert [current for current, _ in persisted] == [8, 9, 10, 11, 12]
    assert persisted == [
        (payload["current"], payload["total"]) for payload in emitted
    ]
    assert attempts.count("Cooldown") == 2
    assert len(emitted) == len(links)
    assert isinstance(results[3], RuntimeError)