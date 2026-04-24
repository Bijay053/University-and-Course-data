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

# T005: JavaScript that finds the "International students" toggle on a
# course page (radio / checkbox / link / button) and clicks it.
# Mirrors Node ``browser-helper.ts`` lines 260-340.
#
# Strategy:
# 1. Direct radio/checkbox: ``input[value*="international" i]`` —
#    most VIT pages put the toggle as a radio button.
# 2. Tab/link/button by visible text: any clickable whose text
#    contains "international" and not already aria-selected/active.
# 3. Aria-controls / data-target wrappers around an "international"
#    label.
#
# Returns true (boolean) when something was clicked, false otherwise.
# The click is fire-and-forget: any errors are swallowed so a missing
# toggle doesn't break the wider browser fetch.
_INTERNATIONAL_TOGGLE_JS = r"""
() => {
  const isHidden = (el) => {
    if (!el) return true;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return true;
    const rect = el.getBoundingClientRect();
    return rect.width === 0 && rect.height === 0;
  };
  const isAlreadyActive = (el) => {
    if (!el) return false;
    if (el.checked === true) return true;
    if (el.getAttribute && el.getAttribute('aria-selected') === 'true') return true;
    if (el.classList && (el.classList.contains('active') ||
        el.classList.contains('selected') || el.classList.contains('is-active'))) {
      return true;
    }
    return false;
  };
  // Pre-click fingerprint: capture state we can compare against after
  // the click to verify the page genuinely toggled to international view
  // (mirrors Node browser-helper before/after-state check). Without this,
  // a nav-menu "International" link could be clicked instead of the
  // intended fee/eligibility toggle.
  const beforeUrl = location.href;
  const beforeBodyLen = (document.body && document.body.innerText || '').length;
  // Strategy 1: input[type=radio|checkbox] whose value/name matches.
  // These are the safest targets — they cannot navigate the page away
  // and almost always belong to a fee/eligibility toggle group.
  const inputs = Array.from(document.querySelectorAll(
    'input[type="radio"], input[type="checkbox"]'));
  for (const input of inputs) {
    const v = (input.value || '').toLowerCase();
    const n = (input.name || '').toLowerCase();
    const id = (input.id || '').toLowerCase();
    if (!/international|overseas|offshore/.test(v + ' ' + n + ' ' + id)) continue;
    if (isHidden(input) || isAlreadyActive(input)) continue;
    try { input.click(); return true; } catch (e) {}
    // If the input is hidden behind a label, click the label instead.
    const label = document.querySelector('label[for="' + input.id + '"]');
    if (label) { try { label.click(); return true; } catch (e) {} }
  }
  // Strategy 2: clickable text element whose text contains "international".
  // Filter aggressively to avoid clicking a nav-menu / footer link that
  // would navigate away from the course page.
  const candidates = Array.from(document.querySelectorAll(
    'button, [role="tab"], [role="button"], li, label, span, div'));
  for (const el of candidates) {
    const txt = (el.textContent || '').trim().toLowerCase();
    if (txt.length === 0 || txt.length > 80) continue;
    // Strict text check — must have "international" as a standalone word
    // with at most one extra word (e.g. "International students").
    if (!/^international(?:\s+(?:students?|fees?|applicants?))?$/.test(txt)) {
      continue;
    }
    if (isHidden(el) || isAlreadyActive(el)) continue;
    // Skip elements wrapped in a nav/header/footer — those are
    // overwhelmingly site navigation, not fee toggles.
    let inNav = false;
    let p = el.parentElement;
    while (p) {
      const tag = p.tagName.toLowerCase();
      if (tag === 'nav' || tag === 'header' || tag === 'footer') { inNav = true; break; }
      p = p.parentElement;
    }
    if (inNav) continue;
    try { el.click(); } catch (e) { continue; }
    // Post-click verification: if the click navigated us away from the
    // course page, we clicked the wrong thing — return false so the
    // Python caller doesn't treat the post-click HTML as the toggle
    // result. (Browser hasn't reloaded yet at this point but will if
    // the click resolved to <a href>; the location.href check catches
    // SPA-style pushState navigations too.)
    if (location.href !== beforeUrl) return false;
    return true;
  }
  return false;
}
"""


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
        click_international: bool = False,
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
                # T005: optional "International students" toggle click.
                # Mirrors Node ``browser-helper.ts`` lines 260-340 — used
                # by VIT pages where domestic fees show by default and
                # the international panel is gated behind a radio /
                # checkbox / link toggle. The JS finds any clickable
                # whose text/value/aria-label matches "international"
                # and isn't already active, then clicks it. Best-effort:
                # any failure during the click is silent — we still
                # return the post-settle HTML.
                if click_international:
                    try:
                        clicked = await page.evaluate(_INTERNATIONAL_TOGGLE_JS)
                        if clicked:
                            # Wait for any post-click XHR / re-render.
                            try:
                                await page.wait_for_load_state(
                                    "networkidle", timeout=5000
                                )
                            except Exception:
                                pass
                            await page.wait_for_timeout(1200)
                    except Exception as exc:
                        log.debug("international toggle click failed on %s: %s", url, exc)
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
