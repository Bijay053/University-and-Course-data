"""Per-course browser fallback (T207).

When the HTTP fetcher returns HTML for a course requirements page but the
page is JavaScript-rendered (Akamai/Cloudflare gate, React SPA, accordions
that load via XHR on click), the english-test extractor sees an empty
table and emits no IELTS/PTE/TOEFL/CAE values. Node's scraper handles this
by re-fetching the same URL through Playwright when the cheerio extractor
returns nothing useful, then re-running the extractor against the rendered
HTML — see ``routes/scrape.ts:11243`` (``perCourseBrowserFallback``).

This module is the Python port of that hook. Public entry-point:
:func:`maybe_browser_refetch`. It only activates when *all* english-test
slots are empty (so we never spend a browser slot on a page that already
parsed cleanly), and it merges using first-write-wins so any non-empty
slot the original extractor populated wins over the browser pass.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from app.services.scraper.browser_pool import pool as browser_pool
from app.services.scraper.extractors import (
    duration,
    english_test,
    fee,
    intake,
    location,
    study_mode,
)
from app.services.scraper.extractors.base import ExtractionResult
from app.services.scraper.extractors._text import compact, html_to_text

# T005: hosts where the per-course browser pass should also click the
# "International students" toggle to surface the international fees /
# admissions panel. Add new hosts here as we encounter them.
_INTERNATIONAL_TOGGLE_HOSTS = (
    "vit.edu.au",
    # Murdoch: "What type of student are you?" Domestic | International toggle.
    # Without clicking International the rendered HTML shows domestic fees only
    # (hides Full course fee, IELTS requirements, intake dates).
    "murdoch.edu.au",
    # UTAS: "DOMESTIC | INTERNATIONAL" tab bar rendered by JS on every course
    # page. The Domestic tab is active by default — static HTML and the initial
    # browser render show CSP / HECS domestic fees only. Clicking "INTERNATIONAL"
    # reveals the international tuition fee, IELTS requirements, and international
    # campus/location availability. The toggle button text is "INTERNATIONAL"
    # (all-caps) which the JS lowercases before matching ^international$.
    "utas.edu.au",
    # University of Newcastle (uni_id 17): right-sidebar "Student type"
    # block has Domestic | International buttons. Domestic is active by
    # default and shows the domestic indicative fee + (for some
    # programs, e.g. Bachelor of Physiotherapy Honours) a different
    # nominal duration (5 years domestic vs 4 years international).
    # Clicking International switches both fee and duration to the
    # international view used in the catalogue.
    "newcastle.edu.au",
)


def _needs_international_toggle(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host.endswith(h) for h in _INTERNATIONAL_TOGGLE_HOSTS)

log = logging.getLogger(__name__)

# PR-5 Bug 3: per-host browser config (wait_until, settle, outer ceiling,
# inner goto timeout). PR-1.5 made `networkidle` + 60s budget the
# universal default to fix VIT's SPA hydration (the english <table>
# rendered via XHR after DCL fired, so the cheap path saw an empty
# skeleton). But ASA / Torrens / similar marketing sites embed
# long-poll widgets (Intercom, Hotjar, GA stream) that prevent the
# network from EVER going idle, so every per-course browser hit on
# those hosts ate the full 60s budget and timed out (prod sweeps
# job_8af4a... ASA 9/9 timeouts, Torrens 22/22 timeouts). Allow-list
# networkidle to SPAs that need it; default everyone else to fast
# `domcontentloaded` with a tight 20s outer ceiling. Add new hosts
# here when a regression sweep proves they need the slow path.
# Issue 1: VIT /vocational/* pages embed a heavy third-party widget that
# prevents networkidle from ever firing, causing every vocational URL to
# sit for the full 30s outer ceiling (10 courses × 30s = 5min wall-time).
# The VIT static fallback (vit_static_extract.py) rescues duration /
# intakes / location from the same static HTML so the end result is fine
# — we skip the browser pass entirely for these paths rather than wasting
# the budget on a guaranteed timeout.
_SKIP_BROWSER_PATH_PREFIXES: dict[str, tuple[str, ...]] = {
    "vit.edu.au": ("/vocational/",),
}

# Hosts for which the browser is ALWAYS skipped because a dedicated static
# extractor handles the full field set (no path restrictions needed).
# CSU: 1.3MB SSR pages already contain fees / IELTS / duration / intakes as
# embedded JS variables.  The browser was causing rate-limiting at concurrency
# ≥5 and never produced better data than the static extractor.
_SKIP_BROWSER_HOSTS: tuple[str, ...] = (
    "study.csu.edu.au",
    # ASAHE publishes English requirements as image-only screenshots (MaSTER.png /
    # Bachelor.png embedded in Webflow HTML).  The browser never reaches networkidle
    # (Cloudflare + analytics prevent it) so every ASAHE course hits the full 60s
    # ceiling and returns nothing.  Vision OCR reads the images directly off the
    # static HTML via _find_english_section_images — skipping the browser saves
    # 9 × 60s ≈ 9 minutes per scrape run with no loss of data.
    "asahe.edu.au",
    # AIT (Academy of Interactive Technology): static HTML fetch succeeds and
    # returns 250KB–1MB per course page, but the headless browser times out every
    # single time (confirmed: 12/12 timeouts at 60s = 12 min wasted per run).
    # The cause is heavy third-party trackers (ait.yourcreative.com.au, HubSpot CDN)
    # that prevent networkidle from ever settling.  More importantly, the per-course
    # browser yields NO additional data for AIT: fees live on /apply (not course
    # pages) and IELTS appears only in HubSpot-hosted PDF course guides.
    # Skipping the browser saves 12 × 60s ≈ 12 minutes per scrape run with
    # zero data loss; the static extractors + central fee page handle all that
    # AIT does publish on its public course detail pages.
    "ait.edu.au",
    # AUT (Auckland University of Technology): static HTML is 60–100KB of
    # fully-rendered SSR content that already contains fees, IELTS, and other
    # fields. The per-course browser times out on every AUT page (13/13 timeouts
    # at 60s in the first full run = 13 min wasted) because AUT embeds heavy
    # third-party trackers and analytics that prevent networkidle from settling.
    # Skipping saves ~60s × n_courses per run with zero data loss — the static
    # extractors already capture all data AUT publishes on course detail pages.
    "aut.ac.nz",
)


def _skip_browser_for_url(url: str) -> bool:
    """Return True for URLs where a host-specific static fallback is
    sufficient and the browser pass is known to always time out."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
    except Exception:
        return False
    # Whole-host skip (e.g. CSU static extractor covers all paths)
    if any(host == h or host.endswith("." + h) for h in _SKIP_BROWSER_HOSTS):
        return True
    for h, prefixes in _SKIP_BROWSER_PATH_PREFIXES.items():
        if host == h or host.endswith("." + h):
            if any(path.startswith(p) for p in prefixes):
                return True
    return False


_NETWORKIDLE_HOSTS: tuple[str, ...] = (
    # Murdoch: heavy React SPA (450KB static → 1MB rendered). Must wait for
    # networkidle before the International toggle click fires correctly, otherwise
    # the toggle target element hasn't mounted yet.
    "murdoch.edu.au",
    # University of Newcastle: Cloudflare-protected Sitecore SPA.  The
    # student-type toggle (Domestic | International) is hydrated AFTER
    # initial render; domcontentloaded fires before the toggle exists in
    # the DOM, so the click is a no-op.  networkidle + 3s settle gives
    # the React hydration time to mount the buttons.
    "newcastle.edu.au",
    # UOW: course details (fee, IELTS, intakes, campus) are loaded via XHR
    # into accordion panels after initial page load. domcontentloaded misses
    # all of it; networkidle + 3s settle is required.
    "uow.edu.au",
    # UniSQ: course detail panels (fees, entry requirements, study modes) are
    # React-rendered after page load. Need networkidle to catch the XHR content.
    "unisq.edu.au",
    # VIT: English table and fee data are XHR-rendered after page load.
    # networkidle with a tighter 30s / 25s budget is sufficient for SPA
    # hydration and avoids over-waiting on vocational pages.
    "vit.edu.au",
    # JCU: handbook pages are server-rendered so domcontentloaded fires
    # correctly, but the default 60s outer ceiling causes huge wall-clock
    # times (49 min for 116 courses) because many pages hit the ceiling
    # before returning content. networkidle with the 30s / 25s budget is
    # sufficient — if networkidle fires early we save time; if the page is
    # blocked by Cloudflare the 30s cap terminates it in half the time.
    "jcu.edu.au",
)

# Hosts that need domcontentloaded + a longer-than-default JS settle window.
# Add hosts here when networkidle is unsuitable (e.g. long-poll analytics
# widgets that prevent idle from firing) but a brief DCL+settle window is
# enough to mount the feature the scraper needs to click.
_DCL_SETTLE_MS_OVERRIDES: dict[str, int] = {
    # UWA (University of Western Australia): Sitecore SXA SPA.
    # networkidle NEVER fires — UWA has persistent analytics/chat
    # connections (askuwa.widget.custhelp.com, GA) that prevent the
    # network from going idle.  goto times out at 25s, the partial-HTML
    # fallback grabs page.content() immediately (no settle), and the
    # Sitecore requirements panel hasn't hydrated yet → rendered=0B.
    # Confirmed live: Masters staging fine from static JSON-LD; Bachelors
    # have NO IELTS in static HTML so rendered=0B = 0 english_test = fail.
    # Fix: domcontentloaded fires in ~1-2s, then 5s settle gives Sitecore
    # time to hydrate the requirements panel — same pattern as VIT.
    # _FORCE_BROWSER_HOSTS (below) ensures browser is called even when
    # static HTML succeeds with duration populated.
    "uwa.edu.au": 5000,
}

# Hosts that need the full 60s / networkidle treatment.
# These are sites that are either genuinely slow from our DigitalOcean
# IP, publish critical data via images (ASA), or use heavy React/Drupal
# frontends that take >30s to reach idle (KBS, CSU).
#
# ASA  — English requirements are image-only; vision OCR can't fire
#         unless the browser fully loads each page.
# KBS  — Drupal-rendered pages take >20s; without rendered HTML
#         Gemini-primary sees only the React shell.
# CSU  — React SPA with 800KB-1.3MB pages; static HTML is a 39-byte
#         shell, so every extractor gets nothing without a full render.
_SLOW_HOSTS: tuple[str, ...] = (
    "asahe.edu.au",
    "kbs.edu.au",
    "study.csu.edu.au",
    # UTAS: Cloudflare-protected. Per-course pages return a JS challenge via
    # HTTP (which is why every URL hits [BROWSER↑] HTTP blocked) and the
    # browser must wait for Cloudflare to issue cf_clearance AND for the
    # JS-rendered International tab to mount.  Default domcontentloaded + 1.5s
    # left UTAS at 116/120 fetch_failed in prod (job_..._utas) because the
    # Cloudflare interstitial hadn't cleared by the time we grabbed
    # page.content().  networkidle + 5s settle (see _NETWORKIDLE_SETTLE_OVERRIDES)
    # gives the challenge time to resolve and yields full course HTML.
    "utas.edu.au",
)


# Per-host overrides for the networkidle settle window.  Default is
# _NETWORKIDLE_SETTLE_MS (3000ms) — bump here for hosts whose post-render
# settle needs more time (e.g. Cloudflare JS challenges that finish 3-8s
# AFTER networkidle reports the network has settled).
_NETWORKIDLE_SETTLE_OVERRIDES: dict[str, int] = {
    "utas.edu.au": 5000,
}


# Hosts where the first browser attempt is allowed to retry once with a
# short backoff when it returns None.  Used for sites where Cloudflare /
# Akamai routinely block the first request but pass the second once a
# session cookie (cf_clearance / akamai_*) has been set by the failed
# attempt.  Keep this list TIGHT — every entry doubles worst-case wall-time
# for that host on a hard outage.
_BROWSER_RETRY_HOSTS: tuple[str, ...] = (
    "utas.edu.au",
)


def should_retry_browser(url: str) -> bool:
    """Return True for URLs whose host is in :data:`_BROWSER_RETRY_HOSTS`.

    Used by the HTTP→browser fallback in single_course.py to decide whether
    to retry once after the first browser attempt returns None.  Cloudflare-
    protected hosts (UTAS) often pass on the second attempt because the first
    attempt's failed request still set cf_clearance.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in _BROWSER_RETRY_HOSTS)

# Hosts whose static HTML contains misleading site-wide IELTS/English
# statements that cause the browser pass to be skipped too early (the
# generic value gets extracted from the static page, marking the slot
# as "populated", so the per-course Entry Requirements tab — which is
# JS-rendered — is never fetched).  For these hosts we ALWAYS run the
# Playwright browser and allow its English-test result to OVERRIDE the
# static value (higher-specificity course page wins over generic footer
# text).  Federation is the canonical example: its static HTML contains
# "minimum IELTS 6.0" in a site-wide section, but course-specific pages
# can require 7.0 or higher.
_FORCE_BROWSER_HOSTS: tuple[str, ...] = (
    "federation.edu.au",
    "une.edu.au",
    # UOW and UniSQ: fee + IELTS data is served via JS-rendered components
    # (accordion panels, dynamic tab content). Static HTML contains only a
    # skeleton and a few meta tags. We ALWAYS render these hosts via Playwright
    # so that fee.extract / english_test.extract / intake.extract etc. see the
    # fully hydrated DOM. The "override" flag lets rendered values overwrite any
    # misleading fragment the static fetcher may have picked up.
    "uow.edu.au",
    "unisq.edu.au",
    # VIT: static HTML renders the Domestic student panel by default, showing
    # the domestic fee. The browser clicks the International toggle which
    # reveals the correct international fee. We must always run the browser
    # (even when english slots are filled from static) and override the fee
    # slot so the international fee replaces the domestic one.
    "vit.edu.au",
    # UTAS: Cloudflare-protected. The Domestic tab is active by default so
    # static HTML shows only CSP/HECS domestic fees. The browser must ALWAYS
    # run (even if IELTS was somehow extracted from static HTML) and click the
    # INTERNATIONAL tab to expose international fees, IELTS requirements and
    # the correct campus/location list. The override flag ensures browser-
    # rendered international values replace any domestic figures picked up
    # during the static pass.
    "utas.edu.au",
    # University of Newcastle: Cloudflare-protected. Static HTML shows
    # the domestic student-type panel by default (domestic indicative
    # fee + domestic nominal duration for programs that differ between
    # student types). The browser must ALWAYS run and click the
    # International toggle to expose the international fee and the
    # correct nominal duration.  Without this, fee + duration stay at
    # the domestic values fleet-wide (real bug 2026-05-15: Bachelor of
    # Physiotherapy Honours staged with 5 Year / A$43,250 instead of
    # the correct 4 years / AUD 46,380 on the international panel).
    "newcastle.edu.au",
    # Macquarie University (uni 277): the admissions pages at
    # www.mq.edu.au/study/find-a-course/courses/<slug> are CF-protected
    # Svelte SPAs. Static HTML is a 200KB+ shell with text_len≈77 (just
    # site-nav icon labels — "open_in_new eStudent" etc.); fee, IELTS,
    # session intake, campus and study mode are all hydrated client-side.
    # mq.yaml sets discovery.use_stealth_browser=true so browser_pool
    # routes fetches through patchright + Xvfb, but without forcing the
    # browser pass + extended extractor here, only english_test runs
    # against the rendered HTML and every other field stays NULL. With
    # this entry, the sparse-static rescue at single_course.py:2766
    # forces a full browser refetch, _is_extended below runs the full
    # extractor suite against the rendered DOM (fee + IELTS + intake +
    # duration + location + study_mode), and the override flag lets
    # rendered values overwrite any junk static values.
    # NOTE: coursehandbook.mq.edu.au URLs are now rewritten to
    # www.mq.edu.au admissions URLs during discovery (see
    # mq_browser_discover._resolve_to_study_urls), so this single host
    # entry covers the entire MQ catalogue.
    "mq.edu.au",
    # UWA (University of Western Australia): removed from _FORCE_BROWSER_HOSTS.
    # Live testing confirmed UWA static HTML DOES contain IELTS (6.5) for
    # Masters/Grad Certs/Grad Dips via regex; forcing browser on all 402
    # courses added ~108 min to the job for zero gain on postgrad pages.
    # Standalone Bachelor pages genuinely have no IELTS anywhere (UWA uses
    # ATAR-based admission, not per-course IELTS) so browser adds nothing
    # there either. Sparse-static rescue (fee+duration both None) won't
    # re-fire because duration IS in static HTML for all UWA pages.
    # _DCL_SETTLE_MS_OVERRIDES entry is kept for the rare pages that do
    # need a browser render (future per-uni toggle if needed).
)

_NETWORKIDLE_SETTLE_MS = 3000
_DEFAULT_SETTLE_MS = 1500
# Outer ceilings keep a single hung page from wedging the Celery worker
# (prod incident: job_2dc0ba6bf4c9 sat at 0/10 for 32min, zero log
# output).
# _SLOW_HOSTS get 60s outer / 50s goto / networkidle.
# _NETWORKIDLE_HOSTS (VIT) get 30s outer / 25s goto / networkidle.
# Default raised to 60s outer / 50s goto after KBS/ASA/CSU all proved
# that the previous 20s ceiling was too aggressive for real education
# sites on our DigitalOcean IP.
_SLOW_OUTER_TIMEOUT_SEC = 60
_SLOW_GOTO_TIMEOUT_MS = 50_000
_NETWORKIDLE_OUTER_TIMEOUT_SEC = 30
_NETWORKIDLE_GOTO_TIMEOUT_MS = 25_000
_DEFAULT_OUTER_TIMEOUT_SEC = 60
_DEFAULT_GOTO_TIMEOUT_MS = 50_000


def _browser_config_for(url: str) -> tuple[str, int, int, int]:
    """Return (wait_until, settle_ms, outer_timeout_sec, goto_timeout_ms)
    for the given URL.

    * Hosts in :data:`_SLOW_HOSTS` (ASA, KBS, CSU) get ``networkidle``
      + 3s settle, 60s outer ceiling, 50s goto.
    * Hosts in :data:`_NETWORKIDLE_HOSTS` get ``networkidle`` + 3s
      settle, 30s outer ceiling, 25s goto.
    * Hosts in :data:`_DCL_SETTLE_MS_OVERRIDES` get ``domcontentloaded``
      + a host-specific settle, 60s outer ceiling, 50s goto.  Used for
      VIT: analytics widgets prevent networkidle from firing, so we use
      domcontentloaded + 6s settle to let the International toggle mount.
    * Everyone else gets ``domcontentloaded`` + 1.5s settle, 60s outer
      ceiling, 50s goto.  The default was raised from 20s after multiple
      Australian education sites proved too slow for the old ceiling.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    if any(host == h or host.endswith("." + h) for h in _SLOW_HOSTS):
        # Per-host settle override (e.g. UTAS Cloudflare needs 5s) wins
        # over the default 3s.
        settle_ms = _NETWORKIDLE_SETTLE_MS
        for h, override_ms in _NETWORKIDLE_SETTLE_OVERRIDES.items():
            if host == h or host.endswith("." + h):
                settle_ms = override_ms
                break
        return (
            "networkidle",
            settle_ms,
            _SLOW_OUTER_TIMEOUT_SEC,
            _SLOW_GOTO_TIMEOUT_MS,
        )
    if any(host == h or host.endswith("." + h) for h in _NETWORKIDLE_HOSTS):
        return (
            "networkidle",
            _NETWORKIDLE_SETTLE_MS,
            _NETWORKIDLE_OUTER_TIMEOUT_SEC,
            _NETWORKIDLE_GOTO_TIMEOUT_MS,
        )
    for h, settle_ms in _DCL_SETTLE_MS_OVERRIDES.items():
        if host == h or host.endswith("." + h):
            return (
                "domcontentloaded",
                settle_ms,
                _DEFAULT_OUTER_TIMEOUT_SEC,
                _DEFAULT_GOTO_TIMEOUT_MS,
            )
    return (
        "domcontentloaded",
        _DEFAULT_SETTLE_MS,
        _DEFAULT_OUTER_TIMEOUT_SEC,
        _DEFAULT_GOTO_TIMEOUT_MS,
    )


# The four slot keys we care about — IELTS overall, PTE overall, TOEFL
# overall, Cambridge Advanced English overall. If any of these are
# already populated we skip the browser pass entirely (the page DID
# render server-side, the extractor just failed on a different field).
_ENGLISH_SLOTS = (
    "ielts_overall",
    "pte_overall",
    "toefl_overall",
    "cambridge_overall",
)


def _all_english_empty(payload: dict[str, Any]) -> bool:
    """Return True when no english-test value has been extracted yet."""
    return all(payload.get(k) in (None, "", 0) for k in _ENGLISH_SLOTS)


# Hosts for which the browser pass should run the FULL extractor suite
# (fee + intake + duration + location + study_mode + english_test) on the
# rendered HTML, not just english_test.  Without this, fee.extract never
# sees the JS-rendered accordion content and always returns empty for UOW/UniSQ.
# VIT: the static HTML shows the Domestic student panel by default.  The
# browser clicks the "International" toggle (see _INTERNATIONAL_TOGGLE_HOSTS)
# which changes the displayed fee from the domestic rate (~$36k/yr) to the
# correct international rate ($48k full course). Without extended extraction
# + override, the domestic fee from the static pass is never replaced.
_EXTENDED_EXTRACT_HOSTS: frozenset[str] = frozenset({
    "uow.edu.au",
    "www.uow.edu.au",
    "unisq.edu.au",
    "www.unisq.edu.au",
    "vit.edu.au",
    "www.vit.edu.au",
    # UTAS: after clicking the INTERNATIONAL tab, the rendered HTML contains
    # the international fee, IELTS requirements, campus list and study mode.
    # Run the full extractor suite so all those fields are populated from
    # the browser-rendered International view rather than the static Domestic HTML.
    "utas.edu.au",
    "www.utas.edu.au",
    # La Trobe: per-course pages are JS-rendered SPAs. Static HTML returns 130KB+
    # of JS shell with text_len=0 (the static-text gate strips all script tags),
    # so Gemini-primary sees nothing. The per-course browser pass DOES render
    # the page successfully (no timeout, returns 100KB+ of rendered HTML), but
    # the standard path below only re-runs english_test against the rendered
    # HTML — fees, durations, intakes, locations and study_mode in the rendered
    # DOM are silently dropped. Symptom: every La Trobe course staged with
    # fee=NULL fleet-wide despite the browser pass running cleanly.
    # Including www.* to match the actual hostname seen on every La Trobe URL.
    "latrobe.edu.au",
    "www.latrobe.edu.au",
    # Macquarie University (uni 277): admissions pages at
    # www.mq.edu.au/study/find-a-course/courses/<slug> are CF-protected
    # Svelte SPAs. Static HTML returns only the page chrome (text_len≈77),
    # so without extended extraction the per-course browser pass would
    # only run english_test against rendered HTML and every other field
    # (fee, intake session, campus, study mode, duration) would stay
    # NULL fleet-wide — symptom verified live 2026-05-25 against the
    # post-coursehandbook-sitemap scrape (200 URLs discovered, every
    # course staged with fee/IELTS/duration/intake BLANK and a
    # `[per-course browser ✓] … filled=[]` log line for every URL).
    # Pair with mq.edu.au entry in _FORCE_BROWSER_HOSTS above.
    "mq.edu.au",
    "www.mq.edu.au",
    # University of Newcastle: after clicking the International toggle, the
    # rendered HTML shows the international indicative fee, the international
    # nominal Duration (FT) value, the international IELTS requirement and the
    # international intake dates.  Re-running the full extractor suite on the
    # rendered DOM (rather than only english_test) is what overwrites the
    # static-pass domestic values with the correct international ones.  Pair
    # with _FORCE_BROWSER_HOSTS + _INTERNATIONAL_TOGGLE_HOSTS + _NETWORKIDLE_HOSTS
    # entries above; all four are required for the toggle to take effect.
    "newcastle.edu.au",
    "www.newcastle.edu.au",
    # Victoria University (VU): course pages are React/SPA shells where the
    # static HTML occasionally returns only the page chrome — fee, duration,
    # intake, IELTS and the real campus list are all hydrated client-side.
    # When that happens, Gemini-primary fills the slots with generic defaults
    # (intake="May", location="Melbourne", IELTS=6) and the browser refetch
    # gate at line 573 sees english slots populated → skips the browser pass
    # entirely → fee/duration stay blank fleet-wide. Including VU here lets
    # the sparse-static rescue path in single_course.py force a full browser
    # extraction that overrides the bogus Gemini fallback values with the
    # real JS-rendered data. Verified live on Bachelor of Dermal Sciences,
    # Diploma of Education Studies, Master of Education, etc.
    "vu.edu.au",
    "www.vu.edu.au",
})

# Field slots that each extended extractor fills — used to guard against
# overwriting a previously-populated value from the static pass.
_EXTENDED_SLOTS: tuple[str, ...] = (
    "international_fee",
    "ielts_overall",
    "pte_overall",
    "toefl_overall",
    "cambridge_overall",
    "intake_months",
    # duration extractor outputs "duration" (float) + "duration_term" (str),
    # not "duration_text" — include both so the override path can replace
    # wrong static values (e.g. "8 Year" from max-candidature sentences).
    "duration",
    "duration_term",
    "duration_text",
    "location_text",
    # location.extract normalises to "course_location" (not "location_text"),
    # so without this entry the rendered-HTML cascade result is silently
    # dropped by the slot filter at ~L556.  Newcastle Master of Nursing
    # (and every other UON Master/GradCert page whose `#degree-location-
    # toggles` div is JS-injected and therefore absent from the static HTML)
    # was reaching the AI fallback with course_location empty; the fallback
    # then filled "Online" which `_sanitise_for_display` stripped via
    # `_REMOVE_VIRTUAL`, leaving the dashboard column blank even though the
    # rendered page clearly shows "Online | Newcastle" toggles.
    "course_location",
    "study_mode",
)

# Keywords used by the rendered-DOM debug sampler.  When a critical field is
# still missing after the full browser extraction we log the 300-char window
# around each keyword so devs can verify the data is/isn't in the rendered DOM.
_DEBUG_KEYWORDS: tuple[str, ...] = (
    "fee",
    "tuition",
    "international",
    "IELTS",
    "English",
    "requirements",
    "ATAR",
    "duration",
    "campus",
    "session",
)


def _rendered_dom_debug(
    rendered: str, url: str, field: str, host: str
) -> dict[str, Any]:
    """Return a structured debug record with 300-char windows around each
    ``_DEBUG_KEYWORDS`` keyword in the rendered text.  Used when a critical
    field is still empty after the full browser extraction pass so that devs
    can distinguish "data not in DOM" from "extractor regex miss"."""
    text = compact(html_to_text(rendered)) if rendered else ""
    snippets: dict[str, str] = {}
    text_lc = text.lower()
    for kw in _DEBUG_KEYWORDS:
        idx = text_lc.find(kw.lower())
        if idx == -1:
            snippets[kw] = "[not found in rendered text]"
        else:
            start = max(0, idx - 120)
            end = min(len(text), idx + 180)
            snippets[kw] = text[start:end].replace("\n", " ")
    return {
        "provider": host,
        "course_url": url,
        "field_name": field,
        "static_text_found": False,
        "rendered_text_found": any(v != "[not found in rendered text]" for v in snippets.values()),
        "rendered_size_chars": len(text),
        "keyword_windows": snippets,
    }


async def _extended_extract(
    rendered: str,
    url: str,
    existing_payload: dict[str, Any],
    override: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run ALL field extractors against rendered HTML and return extracted slots.

    This is called for hosts in :data:`_EXTENDED_EXTRACT_HOSTS` (UOW, UniSQ)
    after the browser has obtained a fully JS-rendered page.

    When ``override=False`` (default): never overwrites a slot that already has
    a truthy value in ``existing_payload`` — correct static extraction wins.

    When ``override=True`` (force-browser hosts like UniSQ/UOW): ALL extractors
    run regardless of existing values, and ALL extracted slots are returned so
    that the caller's ``payload[k] = v`` (direct assignment) can replace any
    wrong static value with the authoritative browser-rendered value.
    """
    extractors = [
        (fee, ["international_fee", "fee_currency", "fee_term", "fee_year"]),
        (english_test, list(_ENGLISH_SLOTS)),
        (intake, ["intake_months"]),
        # duration extractor outputs "duration" + "duration_term", not "duration_text"
        (duration, ["duration", "duration_term"]),
        (location, ["location_text"]),
        (study_mode, ["study_mode"]),
    ]
    filled: dict[str, Any] = {}
    evidence: list[dict[str, Any]] = []

    for extractor_mod, slot_keys in extractors:
        # When override=True (force-browser host), always run every extractor
        # so the rendered DOM can correct any wrong static value.
        # When override=False, skip if ALL slots for this extractor are already
        # populated (existing behavior for non-forced hosts).
        if not override and all(
            existing_payload.get(k) not in (None, "", 0, [])
            for k in slot_keys
        ):
            continue
        try:
            results: list[ExtractionResult] = await extractor_mod.extract(rendered, url)
        except Exception as exc:  # noqa: BLE001 — never abort on extractor failure
            log.warning("extended_extract: %s failed on rendered %s: %s",
                        extractor_mod.__name__, url, exc)
            continue
        for r in results:
            if not r.normalized:
                continue
            for k, v in r.normalized.items():
                if v in (None, "", 0, []):
                    continue
                if k not in _EXTENDED_SLOTS:
                    continue
                # When override=False: only fill slots that are still empty.
                # When override=True: always take the browser value so the
                # caller's direct-assignment can replace wrong static values.
                if not override and existing_payload.get(k) not in (None, "", 0, []):
                    continue
                if k in filled:
                    continue
                filled[k] = v
                evidence.append({
                    "field_key": k,
                    "value": v,
                    # enforce_source_evidence checks for "source_url" AND "snippet"
                    # (not "source_text") — both must be non-empty or the field is
                    # dropped from the payload before staging.
                    "source_url": url,
                    "snippet": (r.snippet or f"browser-rendered: {k}={v}")[:240],
                    "confidence": min(1.0, (r.confidence or 0.6) + 0.05),
                    "method": "per_course_browser_extended",
                })

    return filled, evidence


def _force_browser_for_url(url: str) -> bool:
    """Return True for hosts that always need a browser render, even when
    english-test slots are already populated from static HTML (the static
    value is a generic site-wide statement, not course-specific)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    return any(host == h or host.endswith("." + h) for h in _FORCE_BROWSER_HOSTS)


async def maybe_browser_refetch(
    url: str,
    payload: dict[str, Any],
    *,
    emit: Callable[..., Awaitable[None]] | None = None,
    force: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], str | None, bool]:
    """If the english-test slots are empty, re-fetch the page via
    Playwright and re-run :func:`english_test.extract` against the
    rendered HTML.

    Returns a 4-tuple ``(filled_values, evidence_rows, rendered_html, override)``:
    * ``filled_values`` — slot keys & values to merge into the existing
      payload.  When ``override`` is True the caller should use direct
      assignment rather than ``setdefault`` so the rendered (course-
      specific) value wins over the static (generic) value.
    * ``evidence_rows`` — provenance rows tagged ``method=per_course_browser``.
    * ``rendered_html`` — the Playwright HTML so Gemini-primary and the
      vision-OCR fallback (T208) can use JS-rendered content.  ``None``
      when the browser fetch returned nothing.
    * ``override`` — True when ``force=True`` was passed, meaning the
      caller should let browser values overwrite existing payload slots
      (e.g. Federation whose static HTML has a generic IELTS 6.0 but
      the rendered Entry Requirements tab has the course-specific 7.0).

    All four are empty / ``None`` / False when the slots were already
    populated (and ``force`` is False), or the browser fetch failed.
    """
    if not _all_english_empty(payload) and not force:
        return {}, [], None, False
    # For force-browser hosts (e.g. UniSQ, UOW), the browser ALWAYS runs so
    # JS-rendered fee/IELTS accordion content is captured.  But if BOTH english
    # AND fee are already populated (e.g. from a previous browser pass or a
    # sibling-cache hit), launching a new browser instance would yield nothing
    # new and just waste 10–60 s of wall-clock time per course.  Skip safely.
    if force and not _all_english_empty(payload) and payload.get("international_fee"):
        return {}, [], None, False

    # Issue 1: skip browser pass for paths where a static fallback is
    # sufficient and the browser is known to always time out (e.g. VIT
    # /vocational/* pages). Log a single info line so the sweep log is
    # diagnostic without being noisy.
    if _skip_browser_for_url(url):
        log.info("per_course_browser: skipping browser pass for %s (static fallback sufficient)", url)
        if emit:
            await emit(
                "status",
                f"[per-course browser skipped] {url} — vocational static fallback",
                phase="fallback",
                kind="per_course_browser_skipped",
                url=url,
            )
        return {}, [], None, False

    if emit:
        await emit(
            "status",
            f"[per-course browser ↻] {url}",
            phase="fallback",
            kind="per_course_browser_start",
            url=url,
        )
    wait_until, settle_ms, outer_sec, goto_ms = _browser_config_for(url)
    try:
        rendered = await asyncio.wait_for(
            browser_pool.fetch_html(
                url,
                wait_until=wait_until,
                settle_ms=settle_ms,
                timeout=goto_ms,
                click_international=_needs_international_toggle(url),
            ),
            timeout=outer_sec,
        )
    except asyncio.TimeoutError:
        # Hard ceiling hit. Log a warning BEFORE the abort so the celery
        # journal has a breadcrumb (the prod incident had zero log lines
        # during the 32min hang — even an error would have helped).
        log.warning(
            "browser fallback exceeded %ss on URL %s — aborting this course",
            outer_sec,
            url,
        )
        if emit:
            await emit(
                "status",
                f"timeout: per-course browser exceeded "
                f"{outer_sec}s on {url} — moving on",
                phase="fallback",
                kind="per_course_browser_timeout",
                url=url,
                timeout_seconds=outer_sec,
                level="warn",
            )
        return {}, [], None, False
    except Exception as exc:  # noqa: BLE001 — never abort on browser failure
        log.warning("per_course_browser fetch %s failed: %s", url, exc)
        if emit:
            await emit(
                "status",
                f"[per-course browser ✗] {url}: {exc}",
                phase="fallback",
                kind="per_course_browser_error",
                url=url,
            )
        return {}, [], None, False
    if not rendered:
        if emit:
            await emit(
                "status",
                f"[per-course browser ✗] {url}: empty response",
                phase="fallback",
                kind="per_course_browser_empty",
                url=url,
            )
        return {}, [], None, False

    host = (urlparse(url).hostname or "").lower()
    _is_extended = host in _EXTENDED_EXTRACT_HOSTS

    if _is_extended:
        # UOW / UniSQ: run the FULL extractor suite (fee + IELTS + intake +
        # duration + location + study_mode) against the rendered HTML.  The
        # plain english_test-only path below never sees fee at all.
        # Pass override=force so that force-browser hosts (UniSQ, UOW) let
        # browser-rendered values replace any wrong static-HTML values.
        filled, evidence = await _extended_extract(rendered, url, payload, override=force)

        # ── Rendered-DOM debug for still-missing critical fields ────────
        # When fee or IELTS are still empty after the full render pass, emit
        # structured debug so devs can see what keywords are (or aren't)
        # present in the rendered DOM — distinguishes regex miss from data
        # genuinely absent from the page.
        _critical_missing = {
            k for k in ("international_fee", "ielts_overall")
            if payload.get(k) in (None, "", 0)
            and filled.get(k) in (None, "", 0, [])
        }
        for _cm in _critical_missing:
            _dbg = _rendered_dom_debug(rendered, url, _cm, host)
            log.warning(
                "[RENDERED DOM DEBUG] %s still empty after browser render — "
                "rendered_size=%d chars, %s found in DOM: %s",
                _cm,
                _dbg["rendered_size_chars"],
                "keywords" if _dbg["rendered_text_found"] else "NO keywords",
                url,
            )
            if emit:
                await emit(
                    "status",
                    f"[RENDERED DOM DEBUG] {_cm}: rendered_size={_dbg['rendered_size_chars']}c "
                    f"keywords_found={_dbg['rendered_text_found']} — {url}",
                    phase="fallback",
                    kind="rendered_dom_debug",
                    url=url,
                    field=_cm,
                    debug=_dbg,
                )
    else:
        # Standard path: english_test only.
        # NOTE: english_test.extract is `async def` but contains no await
        # points — it's a pure-CPU regex pipeline. asyncio.wait_for cannot
        # preempt CPU-bound code without yield points. See comment above.
        try:
            results: list[ExtractionResult] = await english_test.extract(rendered, url)
        except Exception as exc:  # noqa: BLE001
            log.warning("english_test re-extract failed on rendered %s: %s", url, exc)
            return {}, [], rendered, force

        filled = {}
        evidence = []
        for r in results:
            if not r.normalized:
                continue
            for k, v in r.normalized.items():
                if v in (None, "", 0):
                    continue
                if k not in _ENGLISH_SLOTS:
                    continue
                if k in filled:
                    continue
                filled[k] = v
                evidence.append(
                    {
                        "field_key": k,
                        "value": v,
                        "source_url": url,
                        # Must be "snippet" (not "source_text") — enforce_source_evidence
                        # in guards.py checks ev.get("snippet"); a "source_text" key
                        # is silently ignored and the field is dropped before staging.
                        "snippet": (r.snippet or f"browser-rendered: {k}={v}")[:240],
                        "confidence": min(1.0, (r.confidence or 0.5) + 0.05),
                        "method": "per_course_browser",
                    }
                )

    if emit:
        def _fmt(k: str) -> str:
            v = filled.get(k) or payload.get(k)
            return str(v) if v not in (None, "", 0, []) else "—"

        _all_filled = sorted(filled.keys())
        await emit(
            "status",
            f"[per-course browser ✓] {url} — "
            f"IELTS={_fmt('ielts_overall')} "
            f"PTE={_fmt('pte_overall')} "
            f"TOEFL={_fmt('toefl_overall')} "
            f"CAE={_fmt('cambridge_overall')} "
            f"fee={_fmt('international_fee')} "
            f"filled={_all_filled}",
            phase="fallback",
            kind="per_course_browser_done",
            url=url,
            filled=_all_filled,
        )

    return filled, evidence, rendered, force
