"""Resource boundaries for synchronous workers, not request/service loops."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

log = logging.getLogger(__name__)
CLOSE_TIMEOUT_SECONDS = 10


async def close_resource(label, close):
    """Teardown must not replace a result, cancellation, or original failure."""
    try:
        await asyncio.wait_for(close(), timeout=CLOSE_TIMEOUT_SECONDS)
    except (Exception, asyncio.CancelledError) as exc:
        log.warning("Could not close %s: %s", label, exc)


@asynccontextmanager
async def owned_engine(engine):
    """Close only the supplied engine, after its connection/session scopes exit."""
    try:
        yield engine
    finally:
        await close_resource("loop-owned DB connections", engine.dispose)


async def _with_cleanup(coro, engine, *, fetch):
    async with owned_engine(engine):
        try:
            return await coro
        finally:
            if fetch:
                # These remove only current-loop cache entries. Local clients
                # (including browser sessions and AI clients) remain service-owned.
                from app.services.scraper.http_fetcher import close_shared_client_for_current_loop
                from app.services.scraper.scrape_do_semaphore import close_client_for_current_loop

                await close_resource("loop-owned HTTP client", close_shared_client_for_current_loop)
                await close_resource("loop-owned Redis client", close_client_for_current_loop)


def run_task_coro(coro, *, engine=None, fetch=False):
    """One fresh-loop boundary per operation, including failure audit writes.

    Invalidate inherited/old-loop connections synchronously at entry; await
    closure of this operation's pool before asyncio.run closes its owning loop.
    Use only in a synchronous worker process, never alongside API requests.
    """
    if engine is None:
        from app.database import engine
    try:
        engine.sync_engine.dispose(close=False)
    except BaseException:
        coro.close()
        raise
    return asyncio.run(_with_cleanup(coro, engine, fetch=fetch))