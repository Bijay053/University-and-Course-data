"""Playwright browser pool with stealth-mode for bot-protected sites (UTAS, etc.)."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from app.config import settings
from app.services.scraper.extractors.curtin_session import (
    playwright_cookies_for_url,
)

# Sentinel returned by fetch_html when the remote server responds with HTTP 429
# (Too Many Requests / Cloudflare rate-limit).  Callers that need to
# distinguish a rate-limit from a generic failure (e.g. to apply a long
# cooldown before retrying) can check ``result is BROWSER_RATE_LIMITED``.
BROWSER_RATE_LIMITED: object = object()


# Per-host browser concurrency caps.  Cloudflare / Akamai-protected sites
# cascade into 503 / "challenge failed" responses when our 10-concurrent
# global pool hammers them in parallel.  UTAS in prod (job_..._utas) showed
# 116/120 fetch_failed when Browser=10 because every parallel request hit
# Cloudflare before the prior request had finished issuing cf_clearance.
# Capping concurrent in-flight requests per host gives the bot-protection
# layer time to process challenges and yields drastically higher staging
# rates with the same total wall-time.
#
# Keep this map TIGHT — every entry serializes one host, slowing it down.
# Only add hosts that empirically choke at Browser=10.
_HOST_CONCURRENCY_CAPS: dict[str, int] = {
    # Lowered from 3 → 2 (2026-05-11) after job_..._utas showed 69/120
    # fetch_failed even with a 2.0s single retry. Cloudflare on UTAS still
    # rate-limited the parallel browser fetches at concurrency=3; halving the
    # in-flight count gives cf_clearance reliably more time to issue.
    "utas.edu.au": 2,
}

log = logging.getLogger(__name__)

# Resolve playwright TimeoutError once at import time so the hot path
# in `fetch_html` doesn't pay an import on every call. In the test
# environment playwright isn't installed (the unit tests stub the
# browser entirely), so we fall back to a private sentinel that real
# code paths will never raise — preserving the narrowed `except`
# semantics introduced by PR-5 Bug 3.
try:  # pragma: no cover - exercised only when playwright is installed
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
except Exception:  # noqa: BLE001
    class PlaywrightTimeoutError(Exception):  # type: ignore[no-redef]
        """Stand-in used when playwright is not importable (test env)."""

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
    // Post-click verification: only reject actual cross-page navigations.
    // Same-page hash changes (e.g. UTAS: href="#tabInternational" updates
    // location.href from ".../course-p3o" to ".../course-p3o#tabInternational")
    // are expected tab-switch behaviour and must NOT cause us to bail.
    const _afterHref = location.href;
    const _beforeBase = beforeUrl.replace(/#.*$/, '');
    const _afterBase = _afterHref.replace(/#.*$/, '');
    if (_afterBase !== _beforeBase) return false;
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
        # Lazily-created per-host semaphores.  Acquired BEFORE the global
        # semaphore in fetch_html so a flood of UTAS requests can't starve
        # the global pool from healthy hosts.
        self._host_sems: dict[str, asyncio.Semaphore] = {}

    def _host_sem_for(self, url: str) -> asyncio.Semaphore | None:
        """Return the per-host concurrency semaphore for ``url`` if the host
        appears in :data:`_HOST_CONCURRENCY_CAPS`, otherwise ``None``."""
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            return None
        for capped_host, cap in _HOST_CONCURRENCY_CAPS.items():
            if host == capped_host or host.endswith("." + capped_host):
                sem = self._host_sems.get(capped_host)
                if sem is None:
                    sem = asyncio.Semaphore(cap)
                    self._host_sems[capped_host] = sem
                return sem
        return None

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

    async def _execute_actions(self, page: Any, actions: list[dict]) -> None:
        """Execute a YAML-driven list of browser interaction actions.

        Each dict in *actions* should have exactly ONE of the following keys:
          click_text   str  — click a visible element whose text matches
                              (case-insensitive). The value "International"
                              uses the smart _INTERNATIONAL_TOGGLE_JS.
          click_css    str  — click the first element matching a CSS selector.
          wait_for     dict — wait for a condition (sub-keys: text | selector).
          expand_text  str  — click an accordion / <details> trigger whose
                              text contains this phrase.
          scroll_to    str  — scroll to a CSS selector or anchor id.
        """
        for step in actions:
            try:
                if "click_text" in step:
                    txt = str(step["click_text"])
                    if txt.lower() == "international":
                        # Use the battle-tested smart JS for the common case
                        clicked = await page.evaluate(_INTERNATIONAL_TOGGLE_JS)
                    else:
                        # Generic click-by-text for any other phrase
                        esc = txt.replace("'", "\\'")
                        js = f"""
() => {{
  const target = '{esc}'.toLowerCase();
  const sel = 'button,[role="tab"],[role="button"],a,label,summary,span,div';
  for (const el of document.querySelectorAll(sel)) {{
    const t = (el.textContent || '').trim().toLowerCase();
    if (t !== target && !t.startsWith(target)) continue;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    let inNav = false;
    let p = el.parentElement;
    while (p) {{
      if ('nav,header,footer'.includes(p.tagName.toLowerCase())) {{ inNav = true; break; }}
      p = p.parentElement;
    }}
    if (inNav) continue;
    el.click();
    return true;
  }}
  return false;
}}"""
                        clicked = await page.evaluate(js)
                    if clicked:
                        try:
                            await page.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(1200)

                elif "click_css" in step:
                    sel = str(step["click_css"])
                    elem = page.locator(sel).first
                    await elem.click(timeout=5000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(1000)

                elif "wait_for" in step:
                    wf = step["wait_for"] or {}
                    if "text" in wf:
                        await page.wait_for_function(
                            f"() => document.body.innerText.includes('{wf['text']}')",
                            timeout=8000,
                        )
                    elif "selector" in wf:
                        await page.wait_for_selector(wf["selector"], timeout=8000)

                elif "expand_text" in step:
                    esc = str(step["expand_text"]).replace("'", "\\'")
                    await page.evaluate(f"""
() => {{
  for (const el of document.querySelectorAll('details,summary,button,[aria-expanded]')) {{
    const t = (el.textContent || '').toLowerCase();
    if (!t.includes('{esc.lower()}')) continue;
    const expanded = el.getAttribute('aria-expanded');
    if (expanded === 'true') continue;
    el.click();
    return true;
  }}
  return false;
}}""")
                    await page.wait_for_timeout(600)

                elif "scroll_to" in step:
                    tgt = str(step["scroll_to"])
                    await page.evaluate(
                        f"() => {{ const el = document.querySelector('{tgt}'); if (el) el.scrollIntoView(); }}"
                    )
                    await page.wait_for_timeout(400)

            except Exception as exc:
                log.debug("action step %r failed on %s: %s", step, page.url, exc)

    async def fetch_html(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        timeout: int = 30000,
        settle_ms: int = 1500,
        click_international: bool = False,
        actions: list[dict] | None = None,
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
        # Stealth opt-in (Macquarie etc.): when the active uni config sets
        # discovery.use_stealth_browser=true, route per-course HTML fetches
        # through patchright + Xvfb instead of the headless playwright pool.
        # Cloudflare on www.mq.edu.au returns 403 to the regular pool but
        # passes the patchright headed-via-Xvfb stack.  Adds ~2-4s per page.
        try:
            from app.services.scraper.stealth_browser import (
                stealth_fetch_html,
                stealth_required,
            )
            if stealth_required():
                stealth_result = await stealth_fetch_html(
                    url, wait_until=wait_until, timeout_ms=timeout,
                    settle_ms=max(settle_ms, 4000),
                )
                # Fallback semantics: only return on actual success.  When
                # stealth returns None (Xvfb missing, CF not solved, patchright
                # crashed) FALL THROUGH to the regular headless pool so we at
                # least get an error response we can detect/log rather than
                # silently losing the page.
                if stealth_result is not None:
                    return stealth_result
                log.warning(
                    "stealth fetch returned None for %s — falling back to regular pool",
                    url,
                )
        except Exception as _stealth_exc:  # noqa: BLE001
            log.warning("stealth fetch wrapper failed for %s: %s — falling back to regular pool", url, _stealth_exc)

        # Per-host concurrency cap (e.g. UTAS=3) — acquired BEFORE the global
        # semaphore inside self.page() so capped hosts never starve the global
        # pool.  None for hosts without a cap (default behaviour preserved).
        host_sem = self._host_sem_for(url)
        if host_sem is not None:
            async with host_sem:
                return await self._fetch_html_inner(
                    url, wait_until=wait_until, timeout=timeout,
                    settle_ms=settle_ms, click_international=click_international,
                    actions=actions,
                )
        return await self._fetch_html_inner(
            url, wait_until=wait_until, timeout=timeout,
            settle_ms=settle_ms, click_international=click_international,
            actions=actions,
        )

    async def _fetch_html_inner(
        self,
        url: str,
        *,
        wait_until: str,
        timeout: int,
        settle_ms: int,
        click_international: bool,
        actions: list[dict] | None = None,
    ) -> str | None:
        try:
            async with self.page() as page:
                # Per-host session priming (currently only Curtin —
                # see extractors/curtin_session.py for rationale). Must
                # happen BEFORE page.goto so the cookie is sent on the
                # very first request, not just on subsequent navigations.
                # Returns [] for every other host.
                _cookies = playwright_cookies_for_url(url)
                if _cookies:
                    try:
                        await page.context.add_cookies(_cookies)
                    except Exception as _cookie_exc:
                        log.warning(
                            "browser cookie prime failed for %s: %s",
                            url, _cookie_exc,
                        )
                # Set referer to look like coming from Google
                await page.set_extra_http_headers({"Referer": "https://www.google.com/"})
                # PR-5 Bug 3: catch the SPECIFIC navigation-timeout case
                # and STILL try to grab whatever DOM is rendered.
                # Marketing sites embed long-poll widgets that prevent
                # `networkidle` from firing, but the english-test
                # table is usually present in the partial HTML —
                # bailing entirely (returning None) was throwing away
                # usable data and forcing a retry loop that never
                # converged. We deliberately narrow this to playwright
                # TimeoutError only: DNS/cert/protocol failures must
                # still propagate as None, and we sniff for Chromium
                # error pages so we don't silently return junk.
                try:
                    resp = await page.goto(url, wait_until=wait_until, timeout=timeout)
                except PlaywrightTimeoutError as goto_exc:
                    log.warning(
                        "browser fetch %s: goto timed out (%s) — trying partial HTML",
                        url, goto_exc,
                    )
                    try:
                        partial = await asyncio.wait_for(
                            page.content(), timeout=3.0
                        )
                    except Exception:
                        return None
                    if not partial or len(partial) < 1024:
                        return None
                    # Cheap Chromium error-page sniff. The interstitials
                    # have a tiny <body class="neterror"> + <title>like
                    # "example.com" or the error code. If we accept these
                    # as success the staged record gets garbage extracted.
                    lowered = partial[:4096].lower()
                    if (
                        "neterror" in lowered
                        or "chrome-error://" in lowered
                        or "err_name_not_resolved" in lowered
                        or "err_connection_" in lowered
                        or "err_cert_" in lowered
                    ):
                        log.warning(
                            "browser fetch %s: partial HTML looks like a Chromium error page — discarding",
                            url,
                        )
                        return None
                    return partial
                if resp is None:
                    log.warning("browser fetch %s: no response", url)
                    return None
                if resp.status == 429:
                    log.warning("browser fetch %s -> %s", url, resp.status)
                    return BROWSER_RATE_LIMITED  # type: ignore[return-value]
                if resp.status >= 400:
                    log.warning("browser fetch %s -> %s", url, resp.status)
                    return None
                # Give Akamai/JS a moment to settle
                await page.wait_for_timeout(settle_ms)
                # ── Browser interaction step executor ────────────────────
                # Two sources of actions (combined into one ordered list):
                #   1. Legacy click_international=True → prepends the smart
                #      International toggle as the first action.
                #   2. YAML-driven `actions` list from extraction.actions
                #      config — click_text, click_css, wait_for, etc.
                _all_actions: list[dict] = []
                if click_international:
                    _all_actions.append({"click_text": "International"})
                if actions:
                    _all_actions.extend(actions)
                if _all_actions:
                    await self._execute_actions(page, _all_actions)
                html = await page.content()
                return html
        except Exception as exc:
            log.error("browser fetch %s failed: %s", url, exc)
            return None

    async def close(self) -> None:
        """Close the Playwright browser and stop the Playwright instance.

        Also resets ``_browser`` / ``_pw`` to ``None`` so that the next
        ``_ensure()`` call re-initialises the pool on a fresh event loop.
        This matters because Playwright objects are bound to the event loop
        in which they were created; if the loop is replaced (new asyncio.run()
        call after SoftTimeLimitExceeded) the old objects must not be reused.
        """
        _b, _p = self._browser, self._pw
        # Reset first so _ensure() always creates fresh objects on the next
        # asyncio.run() even if the close calls below raise.
        self._browser = None
        self._pw = None
        # Also reset the asyncio primitives so they bind to the new event
        # loop when next awaited (avoids "Future attached to different loop").
        self._sem = asyncio.Semaphore(settings.max_browser_concurrency)
        self._lock = asyncio.Lock()
        self._host_sems = {}
        if _b:
            try:
                await _b.close()
            except Exception:  # noqa: BLE001 — closing is best-effort
                pass
        if _p:
            try:
                await _p.stop()
            except Exception:  # noqa: BLE001
                pass


pool = BrowserPool()
