"""Patchright + Xvfb stealth browser for Cloudflare-protected hosts (Macquarie etc.).

Background
==========
The standard ``browser_pool.BrowserPool`` runs Playwright Chromium in
``headless=True`` mode with anti-automation init scripts.  This passes most
bot checks but Cloudflare on www.mq.edu.au still fingerprints headless
Chromium and returns 403 + "Just a moment..." challenge interstitials.

Patchright (https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python) is a
patched Playwright fork that strips the runtime tells (``window.navigator
.webdriver``, the CDP Runtime.enable signature, etc.).  It cracks Cloudflare
on MQ, but ONLY in ``headless=False`` mode — true headless still trips the
challenge.  To run a headed browser inside a server with no display we spawn
an Xvfb virtual display on demand and point Chrome at it via ``DISPLAY``.

Activation
==========
This module is OFF by default and only wired in when a uni's YAML sets
``discovery.use_stealth_browser: true``.  Every other host keeps using the
existing ``BrowserPool`` so we don't pay the ~3s/page Xvfb overhead fleet-wide
and don't drift other unis off the well-tested stealth profile.

Lifecycle
=========
* ``ensure_xvfb()`` spawns Xvfb on ``:99`` once per process (idempotent,
  asyncio-lock guarded).  The Popen is kept alive for the worker lifetime —
  Celery worker restarts will respawn it.
* ``stealth_fetch_html(url, ...)`` is the analogue of
  ``BrowserPool.fetch_html`` — opens a fresh persistent context, navigates,
  and returns HTML.
* ``stealth_discovery_page()`` is an async context manager analogous to
  ``BrowserPool.page()`` — used by ``browser_discover_generic`` when the uni
  opts in.
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import os
import shutil
import subprocess
import tempfile
import weakref
from contextlib import asynccontextmanager
from typing import Optional

log = logging.getLogger(__name__)

# Internal: track the lazily-spawned Xvfb subprocess + which DISPLAY it owns.
# We use :99 by default but probe upward if it's already taken (defensive —
# in dev this prevents collision with a user's existing X session).
_XVFB_PROC: Optional[subprocess.Popen] = None
_XVFB_DISPLAY: Optional[str] = None
_XVFB_SCREEN = "1280x800x24"
_XVFB_MAX_DISPLAY = 119  # probe :99..:119 if :99 is occupied

# Stealth runs a HEADED Chromium per fetch via persistent context.  Cap the
# in-flight count so a parallel Celery prefetch storm doesn't spawn 10 Xvfb
# windows / Chromium processes at once (each ~200-400 MB RSS) and OOM the
# worker.  Headless playwright in BrowserPool gates on
# settings.max_browser_concurrency; the stealth path needs a tighter cap
# because each instance is much heavier.
_STEALTH_MAX_CONCURRENCY = 2

# asyncio primitives bind to the first event loop that awaits them, and Celery
# prefork runs each task in its own asyncio.run() loop — a module-level Lock
# or Semaphore raises "is bound to a different event loop" for the second
# scrape job in the same worker process (same bug as http_fetcher's
# _scrape_do_sem, observed on JCU 2026-07-09).  Keep one instance per loop.
_LOOP_PRIMS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict]" = (
    weakref.WeakKeyDictionary()
)


def _loop_prim(name: str, factory):
    loop = asyncio.get_running_loop()
    per_loop = _LOOP_PRIMS.get(loop)
    if per_loop is None:
        per_loop = {}
        _LOOP_PRIMS[loop] = per_loop
    obj = per_loop.get(name)
    if obj is None:
        obj = factory()
        per_loop[name] = obj
    return obj


def _xvfb_lock() -> asyncio.Lock:
    return _loop_prim("xvfb_lock", asyncio.Lock)


def _stealth_sem() -> asyncio.Semaphore:
    """Per-event-loop semaphore for stealth fetches."""
    return _loop_prim(
        "stealth_sem", lambda: asyncio.Semaphore(_STEALTH_MAX_CONCURRENCY)
    )


def _shutdown_xvfb() -> None:
    """atexit hook — terminate Xvfb so worker reload doesn't leak processes."""
    global _XVFB_PROC
    proc = _XVFB_PROC
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except Exception:
                proc.kill()
    except Exception:
        pass
    finally:
        _XVFB_PROC = None


atexit.register(_shutdown_xvfb)


def _find_xvfb_binary() -> Optional[str]:
    """Return absolute path to Xvfb binary or None if unavailable.

    Uses ``shutil.which`` (PATH lookup) rather than a hardcoded nix-store path
    so we keep working across deploys when the nix derivation hash changes.
    """
    return shutil.which("Xvfb")


async def ensure_xvfb() -> Optional[str]:
    """Ensure an Xvfb virtual display is running and return its DISPLAY string.

    Returns None if Xvfb is not installed or fails to launch (caller should
    fall back to regular headless playwright).  Idempotent and
    concurrency-safe.  Probes ``:99`` upward to ``:119`` to avoid colliding
    with any pre-existing X server (defensive — Replit dev container has
    none, but a user's local machine may).
    """
    global _XVFB_PROC, _XVFB_DISPLAY
    if _XVFB_PROC is not None and _XVFB_PROC.poll() is None and _XVFB_DISPLAY:
        return _XVFB_DISPLAY

    async with _xvfb_lock():
        if _XVFB_PROC is not None and _XVFB_PROC.poll() is None and _XVFB_DISPLAY:
            return _XVFB_DISPLAY

        xvfb = _find_xvfb_binary()
        if not xvfb:
            log.warning(
                "stealth_browser: Xvfb binary not found on PATH — stealth fetch will "
                "return None and caller will fall back to regular pool."
            )
            return None

        last_err: Optional[str] = None
        for n in range(99, _XVFB_MAX_DISPLAY + 1):
            display = f":{n}"
            # Cheap collision check: if the X11 socket exists, skip.
            if os.path.exists(f"/tmp/.X11-unix/X{n}"):
                last_err = f"{display} socket already present"
                continue
            try:
                proc = subprocess.Popen(
                    [xvfb, display, "-screen", "0", _XVFB_SCREEN, "-nolisten", "tcp"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:
                log.error("stealth_browser: failed to spawn Xvfb on %s — %s", display, exc)
                return None
            # Give Xvfb a beat to bind the socket before Chrome connects.
            await asyncio.sleep(1.5)
            if proc.poll() is not None:
                last_err = f"Xvfb on {display} exited immediately (rc={proc.returncode})"
                continue
            _XVFB_PROC = proc
            _XVFB_DISPLAY = display
            log.info("stealth_browser: Xvfb running on %s (pid=%s)", display, proc.pid)
            return display

        log.error(
            "stealth_browser: could not start Xvfb on any display :99..:%d (last err: %s)",
            _XVFB_MAX_DISPLAY, last_err,
        )
        return None


_REAL_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@asynccontextmanager
async def stealth_context():
    """Yield a patchright persistent BrowserContext that defeats Cloudflare.

    Persistent-context mode is the patchright-recommended max-stealth profile
    (the maintainer documents that ``launch_persistent_context`` is more
    fingerprint-clean than ``launch`` + ``new_context`` because no fresh
    profile-creation telltales are visible to the page).
    """
    try:
        from patchright.async_api import async_playwright  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "patchright not installed. Run: uv add patchright && patchright install chromium"
        ) from exc

    display = await ensure_xvfb()
    if display:
        # Inherit env for the Chromium subprocess.  patchright reads DISPLAY
        # from the env at launch time (same as standard playwright).
        os.environ["DISPLAY"] = display

    user_data_dir = tempfile.mkdtemp(prefix="patchright_")
    pw = await async_playwright().start()
    try:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,  # Cloudflare detects headless even with patchright; xvfb supplies display.
            no_viewport=True,
            locale="en-AU",
            timezone_id="Australia/Sydney",
            user_agent=_REAL_UA,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            yield ctx
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
    finally:
        try:
            await pw.stop()
        except Exception:
            pass
        # Clean the persistent profile directory so long-lived workers don't
        # accumulate ~50-100 MB per fetch under /tmp.
        try:
            shutil.rmtree(user_data_dir, ignore_errors=True)
        except Exception:
            pass


async def stealth_fetch_html(
    url: str,
    *,
    wait_until: str = "domcontentloaded",
    timeout_ms: int = 45_000,
    settle_ms: int = 6_000,
) -> Optional[str]:
    """Fetch a single URL through the stealth stack.

    Strategy (cheapest-first):
    1. curl_cffi Chrome TLS impersonation — zero browser overhead, bypasses
       Cloudflare JA3/JA4 fingerprinting in ~50-200 ms.  Free, no API key.
    2. patchright + Xvfb headed Chromium — heavy fallback for sites that
       require a real JS execution environment (e.g. full Turnstile solve).

    Returns the rendered HTML on success or None on failure.  Cloudflare
    challenge interstitials (title "Just a moment...") are treated as failure.
    Concurrency-gated by ``_STEALTH_MAX_CONCURRENCY`` to keep peak headed-
    Chromium processes bounded on the worker.
    """
    # ── Step 1: curl_cffi TLS impersonation (free, fast, no browser) ─────────
    try:
        from app.services.scraper.http_fetcher import fetch_html_cffi
        cffi_result = await fetch_html_cffi(url)
        if cffi_result is not None:
            log.info("stealth_fetch_html %s: curl_cffi bypass succeeded", url)
            return cffi_result
        log.info("stealth_fetch_html %s: curl_cffi returned None — trying patchright+Xvfb", url)
    except Exception as _cffi_exc:
        log.warning("stealth_fetch_html %s: curl_cffi error (%s) — falling back to patchright", url, _cffi_exc)

    # ── Step 2: patchright + Xvfb headed browser ──────────────────────────────
    async with _stealth_sem():
        try:
            async with stealth_context() as ctx:
                page = await ctx.new_page()
                try:
                    resp = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
                except Exception as exc:
                    log.warning("stealth_fetch_html %s: goto failed — %s", url, exc)
                    return None
                if resp is None:
                    log.warning("stealth_fetch_html %s: no response", url)
                    return None
                if resp.status >= 400:
                    log.warning("stealth_fetch_html %s -> HTTP %s", url, resp.status)
                    return None
                await page.wait_for_timeout(settle_ms)
                try:
                    title = (await page.title() or "").strip().lower()
                except Exception:
                    title = ""
                if "just a moment" in title or "attention required" in title:
                    log.warning("stealth_fetch_html %s: Cloudflare challenge not solved", url)
                    return None
                try:
                    return await page.content()
                except Exception as exc:
                    log.warning("stealth_fetch_html %s: content() failed — %s", url, exc)
                    return None
        except Exception as exc:
            log.error("stealth_fetch_html %s: %s", url, exc)
            return None


def stealth_required() -> bool:
    """Return True if the active uni config opts into stealth browsing."""
    try:
        from app.services.scraper.config import get_uni_config
    except Exception:
        return False
    try:
        cfg = get_uni_config()
    except Exception:
        return False
    try:
        return bool(getattr(cfg.discovery, "use_stealth_browser", False))
    except Exception:
        return False
