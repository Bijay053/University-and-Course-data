"""PR-5 Bug 3: per-host browser config (wait_until + settle_ms +
outer ceiling + goto timeout).

PR-1.5 made `networkidle` + 60s budget the universal default to fix
VIT's SPA hydration. But it broke ASA / Torrens / similar marketing
sites that embed long-poll widgets (Intercom / Hotjar / GA stream) —
the network never goes idle, so every per-course browser hit on those
hosts ate the 60s budget and timed out (prod sweeps job_8af4a... ASA
9/9, Torrens 22/22).

The fix was implemented in two phases:

Phase 1 (original PR-5 plan): allow-list networkidle for SPAs only;
  default everyone else to domcontentloaded + 20s.

Phase 2 (post-prod refinement): real sweeps showed 20s was too tight
  for Australian education sites on our DigitalOcean IP, and ASA's
  english requirements are image-only (vision OCR can't fire unless
  the browser fully loads the page). So:
  - Default outer ceiling raised 20s → 60s / 50s goto.
  - ASA + KBS + study.csu.edu.au promoted to _SLOW_HOSTS
    (networkidle + 3s settle + 60s outer / 50s goto).
  - VIT stays in _NETWORKIDLE_HOSTS with a tighter 30s / 25s budget
    (enough for SPA hydration, avoids over-waiting).

These tests assert the CURRENT (phase-2) production behaviour.
"""
from __future__ import annotations

from app.services.scraper.per_course_browser import _browser_config_for


def test_vit_uses_networkidle():
    wait_until, settle_ms, outer_sec, goto_ms = _browser_config_for(
        "https://vit.edu.au/courses/bachelor-of-business"
    )
    assert wait_until == "networkidle"
    assert settle_ms == 3000


def test_vit_subdomain_uses_networkidle():
    # Subdomains of an allow-listed host should inherit the SPA config.
    wait_until, _, _, _ = _browser_config_for("https://www.vit.edu.au/course-list")
    assert wait_until == "networkidle"


def test_vit_gets_30s_outer_ceiling():
    # VIT legitimately needs ~25s for hydration; the tighter budget
    # prevents the vocational-page third-party widget from eating 60s.
    _, _, outer_sec, goto_ms = _browser_config_for(
        "https://vit.edu.au/courses/bachelor-of-business"
    )
    assert outer_sec == 30
    assert goto_ms == 25_000


def test_asa_uses_networkidle_for_vision_ocr():
    # ASA's english requirements are image-only: vision OCR can't fire
    # unless the browser fully loads the page.  ASA is therefore in
    # _SLOW_HOSTS (networkidle + 60s), not on the domcontentloaded path.
    wait_until, settle_ms, outer_sec, goto_ms = _browser_config_for(
        "https://www.asahe.edu.au/courses/bachelor-of-business"
    )
    assert wait_until == "networkidle"
    assert settle_ms == 3000
    assert outer_sec == 60
    assert goto_ms == 50_000


def test_default_outer_timeout_is_60s():
    # Default outer ceiling is 60s after prod evidence showed 20s was
    # too tight for real Australian education sites on our DO IP.
    _, _, outer_sec, goto_ms = _browser_config_for(
        "https://www.torrens.edu.au/courses/design"
    )
    assert outer_sec == 60
    assert goto_ms == 50_000


def test_unknown_host_uses_domcontentloaded():
    # New universities discovered by the scraper default to the safe
    # path. Adding them to _NETWORKIDLE_HOSTS or _SLOW_HOSTS is opt-in.
    for url in (
        "https://www.usq.edu.au/courses/foo",
        "https://www.torrens.edu.au/courses/bar",
        "https://www.csu.edu.au/courses/baz",
        "https://www.utas.edu.au/courses/qux",
    ):
        wait_until, settle_ms, outer_sec, goto_ms = _browser_config_for(url)
        assert wait_until == "domcontentloaded", (
            f"{url} should not opt into networkidle"
        )
        assert settle_ms == 1500
        assert outer_sec == 60
        assert goto_ms == 50_000


def test_malformed_url_falls_back_to_safe_default():
    # Defensive: hostname() can return None on garbage URLs. The helper
    # must not raise and must default to the safe path.
    wait_until, settle_ms, outer_sec, goto_ms = _browser_config_for("not-a-url")
    assert wait_until == "domcontentloaded"
    assert settle_ms == 1500
    assert outer_sec == 60
    assert goto_ms == 50_000


def test_empty_url_falls_back_to_safe_default():
    wait_until, settle_ms, outer_sec, _ = _browser_config_for("")
    assert wait_until == "domcontentloaded"
    assert settle_ms == 1500
    assert outer_sec == 60


def test_csu_study_subdomain_uses_networkidle():
    # study.csu.edu.au is a Vue.js SPA — static HTML is a 39-byte shell.
    # It is in _SLOW_HOSTS: networkidle + 3s settle + 60s outer / 50s goto.
    # (Note: _skip_browser_for_url also returns True for this host, so the
    # browser pass is entirely skipped in practice — but the config helper
    # must still return a valid tuple for hosts that may be added or removed
    # from the skip list in future.)
    wait_until, settle_ms, outer_sec, goto_ms = _browser_config_for(
        "https://study.csu.edu.au/courses/bachelor-accounting"
    )
    assert wait_until == "networkidle"
    assert settle_ms == 3000
    assert outer_sec == 60
    assert goto_ms == 50_000


def test_csu_www_subdomain_uses_domcontentloaded():
    # www.csu.edu.au is a conventional server-rendered site — only the
    # study subdomain is a SPA. The two must not be conflated.
    wait_until, settle_ms, _, _ = _browser_config_for(
        "https://www.csu.edu.au/courses/baz"
    )
    assert wait_until == "domcontentloaded"
    assert settle_ms == 1500


def test_substring_match_does_not_match_unrelated_host():
    # `vit.edu.au` should not match `evit.edu.au` or `vit.edu.au.example.com`.
    # The match logic uses exact-host or `.<host>` suffix to prevent
    # false-positive opt-ins.
    for url in (
        "https://evit.edu.au/courses",
        "https://malicious-vit.edu.au/courses",
    ):
        wait_until, _, outer_sec, _ = _browser_config_for(url)
        assert wait_until == "domcontentloaded", (
            f"{url} should NOT match vit.edu.au allow-list (substring leak)"
        )
        assert outer_sec == 60
