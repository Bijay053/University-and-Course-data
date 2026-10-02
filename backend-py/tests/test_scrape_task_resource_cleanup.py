import asyncio
from pathlib import Path
from types import SimpleNamespace
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
async def test_semaphore_client_closure_does_not_remove_distributed_slots():
    from app.services.scraper import scrape_do_semaphore

    client = AsyncMock()
    loop = asyncio.get_running_loop()
    scrape_do_semaphore._clients[loop] = client
    await scrape_do_semaphore.close_client_for_current_loop()
    client.aclose.assert_awaited_once()
    client.zrem.assert_not_called()
    assert loop not in scrape_do_semaphore._clients


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


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.asyncio
async def test_post_completion_hook_closes_its_separate_database_loop(failure):
    from app.tasks import scrape_tasks

    query = AsyncMock(
        return_value=[],
        side_effect=RuntimeError("query failed") if failure else None,
    )
    dispose = AsyncMock()
    with (
        patch.object(scrape_tasks, "_async_find_all_queued", query),
        patch.object(scrape_tasks, "engine", SimpleNamespace(dispose=dispose)),
    ):
        if failure:
            with pytest.raises(RuntimeError, match="query failed"):
                await scrape_tasks._post_completion_queued_jobs()
        else:
            assert await scrape_tasks._post_completion_queued_jobs() == []
        dispose.assert_awaited_once()


def test_immediate_hook_preserves_dispatch_and_closes_redis():
    from app.tasks import scrape_tasks
    from unittest.mock import MagicMock

    redis = MagicMock()
    redis.set.return_value = True
    with (
        patch.object(scrape_tasks, "_run_db_coro", return_value=[
            ("business-job", "scrape", 0),
        ]) as run,
        patch.object(scrape_tasks, "_get_redis", return_value=redis),
        patch.object(scrape_tasks.scrape_university, "delay") as dispatch,
    ):
        scrape_tasks._immediate_requeue_hook()
        # The mocked runner does not consume the coroutine.
        run.call_args.args[0].close()
        dispatch.assert_called_once_with("business-job")
        redis.close.assert_called_once()