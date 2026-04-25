"""PR-5 Bug 3: per-host browser config (wait_until + settle_ms +
outer ceiling + goto timeout).

PR-1.5 made `networkidle` + 60s budget the universal default to fix
VIT's SPA hydration. But it broke ASA / Torrens / similar marketing
sites that embed long-poll widgets (Intercom / Hotjar / GA stream) —
the network never goes idle, so every per-course browser hit on those
hosts ate the 60s budget and timed out (prod sweeps job_8af4a... ASA
9/9, Torrens 22/22).

Fix: allow-list networkidle for SPAs that need it, default everyone
else to fast `domcontentloaded` + 1.5s settle, with a tight 20s outer
ceiling so a hung widget can't wedge the worker.
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
    # VIT legitimately needs ~25s for hydration; 20s would over-cancel.
    _, _, outer_sec, goto_ms = _browser_config_for(
        "https://vit.edu.au/courses/bachelor-of-business"
    )
    assert outer_sec == 30
    assert goto_ms == 25_000


def test_asa_uses_domcontentloaded():
    # ASA was the trigger for this fix — long-poll widgets keep the
    # network busy forever, so domcontentloaded is the only safe default.
    wait_until, settle_ms, _, _ = _browser_config_for(
        "https://www.asahe.edu.au/courses/bachelor-of-business"
    )
    assert wait_until == "domcontentloaded"
    assert settle_ms == 1500


def test_default_outer_timeout_is_20s():
    # User explicitly requested 15-20s, not 60s. Default ceiling is 20s
    # for non-VIT hosts so a hung XHR widget can't wedge the worker.
    _, _, outer_sec, goto_ms = _browser_config_for(
        "https://www.torrens.edu.au/courses/design"
    )
    assert outer_sec == 20
    assert goto_ms == 15_000


def test_unknown_host_uses_domcontentloaded():
    # New universities discovered by the scraper default to the safe
    # path. Adding them to _NETWORKIDLE_HOSTS is opt-in only.
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
        assert outer_sec == 20
        assert goto_ms == 15_000


def test_malformed_url_falls_back_to_safe_default():
    # Defensive: hostname() can return None on garbage URLs. The helper
    # must not raise and must default to the safe path.
    wait_until, settle_ms, outer_sec, goto_ms = _browser_config_for("not-a-url")
    assert wait_until == "domcontentloaded"
    assert settle_ms == 1500
    assert outer_sec == 20
    assert goto_ms == 15_000


def test_empty_url_falls_back_to_safe_default():
    wait_until, settle_ms, outer_sec, goto_ms = _browser_config_for("")
    assert wait_until == "domcontentloaded"
    assert settle_ms == 1500
    assert outer_sec == 20


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
        assert outer_sec == 20
