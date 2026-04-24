"""Playwright browser pool with stealth-mode for bot-protected sites (UTAS, etc.)."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from app.config import settings

log = logging.getLogger(__name__)

# Real Chrome 124 on macOS — matches UA we set
_REAL_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


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
            # Launch with flags that defeat common bot-detection checks
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-web-security",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )

    @asynccontextmanager
    async def page(self):
        await self._ensure()
        async with self._sem:
            ctx = await self._browser.new_context(  # type: ignore[union-attr]
                user_agent=_REAL_UA,
                viewport={"width": 1920, "height": 1080},
                locale="en-AU",
                timezone_id="Australia/Sydney",
                extra_http_headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-AU,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Sec-Ch-Ua": '"Chromium";v="124", "Not-A.Brand";v="99"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"macOS"',
                    "Upgrade-Insecure-Requests": "1",
                },
            )
            # Hide webdriver flag — most basic Akamai check
            await ctx.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-AU','en']});
                window.chrome = {runtime: {}};
                """
            )
            page = await ctx.new_page()
            try:
                yield page
            finally:
                await ctx.close()

    async def fetch_html(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        timeout: int = 30000,
        settle_ms: int = 1500,
    ) -> str | None:
        """Fetch a URL via real browser and return HTML. Returns None on failure.

        ``wait_until`` controls how long Playwright waits for the page event:
            * ``"domcontentloaded"`` — fast (default, used by discovery).
            * ``"load"`` — waits for window.onload (CSS, images).
            * ``"networkidle"`` — waits for ≥500ms with no in-flight requests.
              Use this for JS-heavy SPAs that render the requirements table
              after an XHR (VIT, etc.). Costs 1–3s extra per page but is
              necessary for the per-course fallback (T207) — without it we
              see the pre-render skeleton and extract empty english slots.

        ``settle_ms`` is an extra static wait after the load event fires.
        Defaults to 1500ms; bump to ~3000ms for SPA-style pages where the
        requirements table is hydrated client-side after the load event
        completes (PR-1.5 prod regression: VIT MBA pages returned empty
        from the browser fallback because the table hadn't hydrated yet).
        """
        try:
            async with self.page() as page:
                # Set referer to look like coming from Google
                await page.set_extra_http_headers({"Referer": "https://www.google.com/"})
                resp = await page.goto(url, wait_until=wait_until, timeout=timeout)
                if resp is None:
                    log.warning("browser fetch %s: no response", url)
                    return None
                if resp.status >= 400:
                    log.warning("browser fetch %s -> %s", url, resp.status)
                    return None
                # Give Akamai/JS a moment to settle
                await page.wait_for_timeout(settle_ms)
                html = await page.content()
                return html
        except Exception as exc:
            log.error("browser fetch %s failed: %s", url, exc)
            return None

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()


pool = BrowserPool()
