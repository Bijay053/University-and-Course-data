"""Tests for the Macquarie patchright + Xvfb stealth-browser opt-in.

Background
==========
Macquarie's www.mq.edu.au sits behind Cloudflare which returns HTTP 403
+ "Just a moment..." challenge interstitials to:
  * plain HTTP (httpx)
  * the regular headless Playwright pool in ``browser_pool.BrowserPool``

A 2026-05-25 investigation confirmed that ``patchright`` (a patched
Playwright fork) running in ``headless=False`` mode against an Xvfb
virtual display cracks the challenge cleanly (HTTP 200, 189 anchors,
real title "Find a course | Macquarie University").  The stealth path
is wired in via:

  * ``backend-py/app/services/scraper/stealth_browser.py`` — Xvfb +
    patchright lifecycle.
  * ``backend-py/app/services/scraper/config/schema.py`` — new
    ``DiscoveryConfig.use_stealth_browser`` bool flag.
  * ``backend-py/app/services/scraper/browser_discover_generic.py`` —
    swap pool for stealth when flag is on.
  * ``backend-py/app/services/scraper/browser_pool.py`` — route
    ``fetch_html`` per-course fetches through stealth when flag is on.
  * ``backend-py/scraper_config/unis/mq.yaml`` — flag enabled for MQ.

These tests are unit-level pins on the wiring (schema flag, YAML flag,
runtime opt-in detection).  No real Chromium is launched.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.config.schema import DiscoveryConfig, UniConfig


# ── Schema-level pins ─────────────────────────────────────────────────────


def test_discovery_config_use_stealth_browser_default_false():
    """The flag must default to False so existing unis are unaffected."""
    cfg = DiscoveryConfig()
    assert cfg.use_stealth_browser is False


def test_discovery_config_use_stealth_browser_accepts_true():
    cfg = DiscoveryConfig(use_stealth_browser=True)
    assert cfg.use_stealth_browser is True


# ── Macquarie YAML pins ───────────────────────────────────────────────────


_MQ_YAML = (
    Path(__file__).resolve().parents[1] / "scraper_config" / "unis" / "mq.yaml"
)


def test_mq_yaml_file_exists():
    assert _MQ_YAML.exists(), f"Expected MQ config at {_MQ_YAML}"


def test_mq_yaml_enables_stealth_browser():
    data = yaml.safe_load(_MQ_YAML.read_text())
    discovery = data.get("discovery") or {}
    assert discovery.get("use_stealth_browser") is True, (
        "mq.yaml must enable use_stealth_browser=true so the patchright + "
        "Xvfb path is used for Cloudflare-protected www.mq.edu.au"
    )


def test_mq_yaml_keeps_always_browser_discover():
    """Stealth doesn't replace browser-discover — it augments it."""
    data = yaml.safe_load(_MQ_YAML.read_text())
    discovery = data.get("discovery") or {}
    assert discovery.get("always_browser_discover") is True


def test_mq_loaded_uni_config_has_stealth():
    """End-to-end: load_uni_config(slug='mq', ...) must surface the flag."""
    cfg = load_uni_config(
        slug="mq",
        name="Macquarie University",
        scrape_url="https://www.mq.edu.au/study/find-a-course",
    )
    assert cfg.discovery.use_stealth_browser is True


def test_other_uni_config_does_not_enable_stealth():
    """Regression fence — only MQ opts in.  ANU, UNE, UOW etc. must NOT
    accidentally route through stealth (which would add ~3s/page and is
    unnecessary for hosts where regular headless playwright works fine)."""
    unis_root = Path(__file__).resolve().parents[1] / "scraper_config" / "unis"
    for slug in ("anu", "une", "uow", "qut", "curtin", "ecu"):
        yaml_path = unis_root / f"{slug}.yaml"
        if not yaml_path.exists():
            continue
        data = yaml.safe_load(yaml_path.read_text()) or {}
        discovery = data.get("discovery") or {}
        assert discovery.get("use_stealth_browser") in (None, False), (
            f"{slug}.yaml must NOT enable use_stealth_browser — only "
            "Cloudflare-bot-protected hosts should opt in"
        )


# ── Runtime opt-in detection ──────────────────────────────────────────────


def test_stealth_required_false_when_no_uni_context(monkeypatch):
    """When no uni config is active (e.g. ad-hoc scrape, REPL),
    stealth_required() must return False so we don't accidentally launch
    Xvfb + patchright."""
    from app.services.scraper import config as config_mod
    from app.services.scraper.stealth_browser import stealth_required

    # Force get_uni_config to raise (simulates "no context set")
    def _boom():
        raise LookupError("no uni config")

    monkeypatch.setattr(config_mod, "get_uni_config", _boom)
    assert stealth_required() is False


def _make_cfg(stealth: bool) -> UniConfig:
    return UniConfig(
        slug="test",
        name="Test University",
        base_url="https://example.edu/",
        scrape_url="https://example.edu/courses",
        discovery=DiscoveryConfig(use_stealth_browser=stealth),
    )


def test_stealth_required_true_when_flag_on(monkeypatch):
    from app.services.scraper import config as config_mod
    from app.services.scraper.stealth_browser import stealth_required

    monkeypatch.setattr(config_mod, "get_uni_config", lambda: _make_cfg(True))
    assert stealth_required() is True


def test_stealth_required_false_when_flag_off(monkeypatch):
    from app.services.scraper import config as config_mod
    from app.services.scraper.stealth_browser import stealth_required

    monkeypatch.setattr(config_mod, "get_uni_config", lambda: _make_cfg(False))
    assert stealth_required() is False


# ── Xvfb binary discovery (won't actually spawn in tests) ────────────────


def test_find_xvfb_binary_returns_path_or_none():
    """Xvfb must be installed in this environment; if absent, the helper
    must return None (not raise) so callers can fall back gracefully."""
    from app.services.scraper.stealth_browser import _find_xvfb_binary

    path = _find_xvfb_binary()
    # In the Replit dev container we install xorg.xvfb via
    # installSystemDependencies, so this should resolve.  On a fresh
    # checkout it may be missing — accept None too, but assert the contract.
    assert path is None or isinstance(path, str)
    if path is not None:
        assert Path(path).name == "Xvfb"


# ── Module exports ────────────────────────────────────────────────────────


def test_stealth_browser_module_exports():
    """Public API surface stays stable for browser_pool / discover_generic
    callsites."""
    import app.services.scraper.stealth_browser as sb

    for name in (
        "ensure_xvfb",
        "stealth_context",
        "stealth_fetch_html",
        "stealth_required",
    ):
        assert hasattr(sb, name), f"stealth_browser missing public symbol: {name}"


# ── Code-review fixes: concurrency, fallback, lifecycle pins ─────────────


def test_stealth_concurrency_cap_is_tight():
    """Stealth runs HEADED Chromium per fetch (~200-400 MB RSS each) so the
    in-flight cap MUST stay tight (≤ 4) — a regression here could OOM a
    Celery worker on parallel Macquarie scrapes."""
    from app.services.scraper.stealth_browser import _STEALTH_MAX_CONCURRENCY

    assert 1 <= _STEALTH_MAX_CONCURRENCY <= 4, (
        f"stealth concurrency cap {_STEALTH_MAX_CONCURRENCY} is too loose — "
        "each instance launches its own Xvfb-backed headed Chromium"
    )


def test_stealth_sem_is_per_event_loop():
    """_stealth_sem() must return the SAME Semaphore within one event loop
    (so in-flight stealth fetches contend for the cap) but a FRESH one on a
    new loop — asyncio primitives bind to the first loop that awaits them,
    and Celery prefork runs each task in its own asyncio.run() loop (JCU
    whole-job discovery failure, 2026-07-09)."""
    import asyncio

    from app.services.scraper.stealth_browser import _stealth_sem

    async def grab():
        return _stealth_sem()

    loop1 = asyncio.new_event_loop()
    try:
        s1 = loop1.run_until_complete(grab())
        s2 = loop1.run_until_complete(grab())
    finally:
        loop1.close()
    loop2 = asyncio.new_event_loop()
    try:
        s3 = loop2.run_until_complete(grab())
    finally:
        loop2.close()
    assert s1 is s2, "same loop must reuse its semaphore"
    assert s3 is not s1, "a new event loop must get a fresh semaphore"


def test_atexit_handler_registered():
    """The Xvfb shutdown hook must be wired into atexit so worker reload
    doesn't leak orphan X server processes."""
    import atexit

    from app.services.scraper.stealth_browser import _shutdown_xvfb

    # atexit doesn't expose a public list of handlers; on CPython we can
    # peek at the private list as a best-effort regression fence.
    handlers = getattr(atexit, "_exithandlers", None)
    if handlers is None:
        # Fallback: at least assert the function is callable and idempotent
        # when no Xvfb is running.
        _shutdown_xvfb()
        _shutdown_xvfb()
        return
    assert any(h[0] is _shutdown_xvfb for h in handlers), (
        "_shutdown_xvfb must be registered with atexit"
    )


def test_browser_pool_falls_back_when_stealth_returns_none(monkeypatch):
    """When stealth_fetch_html returns None, BrowserPool.fetch_html MUST
    fall through to the regular headless pool (not silently return None).
    This was a code-review finding — the original wiring returned None
    without trying the fallback, losing pages on transient Xvfb failures."""
    import asyncio

    from app.services.scraper import browser_pool as bp
    from app.services.scraper import stealth_browser as sb
    from app.services.scraper.browser_pool import BrowserPool

    async def fake_stealth_fetch_html(*args, **kwargs):
        return None

    fallback_called = {"n": 0}

    async def fake_inner(
        self, url, *, wait_until, timeout, settle_ms, click_international,
        actions=None,
    ):
        fallback_called["n"] += 1
        return "<html>fallback</html>"

    monkeypatch.setattr(sb, "stealth_fetch_html", fake_stealth_fetch_html)
    monkeypatch.setattr(sb, "stealth_required", lambda: True)
    monkeypatch.setattr(BrowserPool, "_fetch_html_inner", fake_inner)

    pool = BrowserPool()
    result = asyncio.run(pool.fetch_html("https://www.mq.edu.au/test"))

    assert result == "<html>fallback</html>"
    assert fallback_called["n"] == 1, (
        "fetch_html must call _fetch_html_inner as fallback when stealth returns None"
    )


def test_browser_pool_uses_stealth_result_when_non_none(monkeypatch):
    """Conversely, when stealth succeeds the result MUST be returned without
    invoking the regular pool (avoids redundant cost)."""
    import asyncio

    from app.services.scraper import stealth_browser as sb
    from app.services.scraper.browser_pool import BrowserPool

    async def fake_stealth_fetch_html(*args, **kwargs):
        return "<html>stealth-success</html>"

    inner_called = {"n": 0}

    async def fake_inner(self, *a, **kw):
        inner_called["n"] += 1
        return "<html>should-not-be-called</html>"

    monkeypatch.setattr(sb, "stealth_fetch_html", fake_stealth_fetch_html)
    monkeypatch.setattr(sb, "stealth_required", lambda: True)
    monkeypatch.setattr(BrowserPool, "_fetch_html_inner", fake_inner)

    pool = BrowserPool()
    result = asyncio.run(pool.fetch_html("https://www.mq.edu.au/test"))
    assert result == "<html>stealth-success</html>"
    assert inner_called["n"] == 0, (
        "fetch_html must NOT invoke the regular pool when stealth succeeds"
    )


def test_browser_discover_generic_imports_asynccontextmanager():
    """Regression fence — code review caught a missing
    `from contextlib import asynccontextmanager` import that would have
    crashed every browser_discover_generic() invocation with NameError."""
    import app.services.scraper.browser_discover_generic as bdg

    assert hasattr(bdg, "asynccontextmanager"), (
        "browser_discover_generic must import asynccontextmanager at module top"
    )


def test_open_page_propagates_post_yield_exceptions(monkeypatch):
    """Code-review fix pin: _open_page (the inline @asynccontextmanager
    inside browser_discover_generic) must ONLY fall back on init failure.
    Exceptions raised by the caller AFTER the yield (e.g. page.goto failing
    mid-discovery) must propagate — wrapping yield in try/except would
    silently retry whole-page errors against the regular pool and mask bugs."""
    import asyncio
    from contextlib import asynccontextmanager

    # Replicate the post-fix _open_page contract in miniature so the test
    # asserts the structural pattern rather than reaching into the closure.
    @asynccontextmanager
    async def init_only_fallback():
        try:
            obj = "stealth-page"
        except Exception:
            obj = None
        if obj is not None:
            try:
                yield obj
            finally:
                pass
            return
        yield "fallback-page"

    async def _run():
        async with init_only_fallback() as page:
            assert page == "stealth-page"
            raise RuntimeError("caller-side boom")

    # Post-yield exception MUST escape, not be swallowed.
    with pytest.raises(RuntimeError, match="caller-side boom"):
        asyncio.run(_run())

    # Structural check on the real implementation: search for the
    # try/except pattern with an `else:` clause that contains the yield.
    # This is what guarantees init-only fallback semantics — wrapping the
    # yield in the except path would put fallback logic on every error.
    import re
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "app" / "services" / "scraper" / "browser_discover_generic.py"
    ).read_text()
    # The fix uses `stealth_cm.__aenter__()` ... `else:` ... `yield page`.
    # Pin the structural marker rather than exact whitespace.
    assert "stealth_cm.__aenter__()" in src, (
        "browser_discover_generic must enter stealth context via explicit "
        "__aenter__/__aexit__ so post-yield exceptions are not caught by "
        "the init-failure fallback handler"
    )
    assert re.search(r"else:\s*\n\s*try:\s*\n\s*yield page", src), (
        "browser_discover_generic must yield page inside the `else:` branch "
        "of the init try/except, so caller-side exceptions propagate"
    )
