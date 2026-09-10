import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_shared_http_client_is_closed_and_removed_for_current_loop():
    from app.services.scraper import http_fetcher

    client = AsyncMock()
    client.is_closed = False
    loop = asyncio.get_running_loop()
    http_fetcher._LOOP_CLIENTS[loop] = client

    await http_fetcher.close_shared_client_for_current_loop()

    client.aclose.assert_awaited_once()
    assert loop not in http_fetcher._LOOP_CLIENTS


@pytest.mark.asyncio
async def test_async_scrape_closes_loop_resources_when_scrape_raises():
    from app.tasks import scrape_tasks

    browser_close = AsyncMock()
    http_close = AsyncMock()

    with (
        patch.object(scrape_tasks, "AsyncSessionLocal") as session_factory,
        patch.object(scrape_tasks, "run_scrape", AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(scrape_tasks, "engine") as mock_engine,
        patch(
            "app.services.scraper.browser_pool.pool.close",
            browser_close,
        ),
        patch(
            "app.services.scraper.http_fetcher.close_shared_client_for_current_loop",
            http_close,
        ),
    ):
        mock_engine.dispose = AsyncMock()
        session_factory.return_value.__aenter__ = AsyncMock(return_value=object())
        session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(RuntimeError, match="boom"):
            await scrape_tasks._async_scrape("job_test")

    browser_close.assert_awaited_once()
    http_close.assert_awaited_once()
    mock_engine.dispose.assert_awaited_once()


def test_production_worker_bounds_descriptor_pressure():
    service = (
        Path(__file__).resolve().parents[1] / "deploy" / "uni-celery.service"
    ).read_text()

    assert "LimitNOFILE=65536" in service
    assert "--max-tasks-per-child=10" in service