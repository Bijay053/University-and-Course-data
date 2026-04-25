"""PR-5 Bug 3: per-host wait_until config for the per-course browser.

PR-1.5 made `networkidle` the universal default to fix VIT's SPA
hydration. But it broke ASA-style sites that embed long-poll widgets
(Intercom / Hotjar / GA stream) — the network never goes idle, so every
per-course browser hit on those hosts ate the 60s budget and timed out
(prod sweep job_8af4a..., 9/9 ASA URLs).

Fix: allow-list networkidle for SPAs that need it, default everyone
else to fast `domcontentloaded` + 1.5s settle.
"""
from __future__ import annotations

from app.services.scraper.per_course_browser import _browser_config_for


def test_vit_uses_networkidle():
    wait_until, settle_ms = _browser_config_for(
        "https://vit.edu.au/courses/bachelor-of-business"
    )
    assert wait_until == "networkidle"
    assert settle_ms == 3000


def test_vit_subdomain_uses_networkidle():
    # Subdomains of an allow-listed host should inherit the SPA config.
    wait_until, _ = _browser_config_for("https://www.vit.edu.au/course-list")
    assert wait_until == "networkidle"


def test_asa_uses_domcontentloaded():
    # ASA was the trigger for this fix — long-poll widgets keep the
    # network busy forever, so domcontentloaded is the only safe default.
    wait_until, settle_ms = _browser_config_for(
        "https://www.asahe.edu.au/courses/bachelor-of-business"
    )
    assert wait_until == "domcontentloaded"
    assert settle_ms == 1500


def test_unknown_host_uses_domcontentloaded():
    # New universities discovered by the scraper default to the safe
    # path. Adding them to _NETWORKIDLE_HOSTS is opt-in only.
    for url in (
        "https://www.usq.edu.au/courses/foo",
        "https://www.torrens.edu.au/courses/bar",
        "https://www.csu.edu.au/courses/baz",
        "https://www.utas.edu.au/courses/qux",
    ):
        wait_until, settle_ms = _browser_config_for(url)
        assert wait_until == "domcontentloaded", f"{url} should not opt into networkidle"
        assert settle_ms == 1500


def test_malformed_url_falls_back_to_safe_default():
    # Defensive: hostname() can return None on garbage URLs. The helper
    # must not raise and must default to the safe path.
    wait_until, settle_ms = _browser_config_for("not-a-url")
    assert wait_until == "domcontentloaded"
    assert settle_ms == 1500


def test_empty_url_falls_back_to_safe_default():
    wait_until, settle_ms = _browser_config_for("")
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
        wait_until, _ = _browser_config_for(url)
        assert wait_until == "domcontentloaded", (
            f"{url} should NOT match vit.edu.au allow-list (substring leak)"
        )
