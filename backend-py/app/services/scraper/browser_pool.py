"""Playwright browser pool. STUB until Playwright is installed on the worker.

Keeping the import inside the methods means the FastAPI process can boot
without Playwright present (it isn't needed for read endpoints).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from app.config import settings

log = logging.getLogger(__name__)


class BrowserPool:
    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(settings.max_browser_concurrency)
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def _ensure(self):
        if self._browser is not None:
            return
        async with self._lock:
            if self._browser is not None:
                return
            try:
                from playwright.async_api import async_playwright  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "Playwright not installed. Run: pip install playwright && playwright install chromium"
                ) from exc
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=True)

    @asynccontextmanager
    async def page(self):
        await self._ensure()
        async with self._sem:
            ctx = await self._browser.new_context(  # type: ignore[union-attr]
                user_agent=(
                    "Mozilla/5.0 (Linux; UniportalBot/1.0) AppleWebKit/537.36 Chrome/124"
                )
            )
            page = await ctx.new_page()
            try:
                yield page
            finally:
                await ctx.close()

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()


pool = BrowserPool()
