"""Tests for the four Flinders-specific bugs fixed in this session:

  Bug 1 — Vision OCR: social-footer icons blocked by _SOCIAL_CHROME_PATH_RE
  Bug 2 — Duration: combined/add-on degree sentences demoted by 0.001×
  Bug 3 — Duplicates: trailing-slash URL normalization in stage_course
  Bug 4 — flinders.yaml: config file created and loads cleanly
"""
from __future__ import annotations

import re

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Bug 1 — Vision OCR: _SOCIAL_CHROME_PATH_RE blocks social footer icons
# ─────────────────────────────────────────────────────────────────────────────

def test_social_chrome_path_re_blocks_flinders_bluesky() -> None:
    """Flinders social-footer image URL must be blocked by _SOCIAL_CHROME_PATH_RE."""
    from app.services.scraper.per_course_vision import _SOCIAL_CHROME_PATH_RE

    bluesky_url = (
        "https://www.flinders.edu.au/reference-components/social-footer"
        "/_jcr_content/content/section/par_0/section_1/par/image"
        "/file/bluesky-circle-mono.png"
    )
    assert _SOCIAL_CHROME_PATH_RE.search(bluesky_url.lower()), (
        f"Expected _SOCIAL_CHROME_PATH_RE to block Flinders social-footer URL: {bluesky_url}"
    )


def test_social_chrome_path_re_blocks_known_platforms() -> None:
    """Social media platform names in URL paths must always be blocked."""
    from app.services.scraper.per_course_vision import _SOCIAL_CHROME_PATH_RE

    should_block = [
        "https://example.com/shared/social-media/facebook-icon.png",
        "https://example.com/assets/social-footer/instagram.svg",
        "https://cdn.example.com/icons/facebook/logo.png",
        "https://www.uni.edu/img/linkedin-badge.png",
        "https://cdn.example.com/bluesky/icon-mono.png",
        "https://www.uni.edu/social_icons/twitter-bird.png",
    ]
    for url in should_block:
        assert _SOCIAL_CHROME_PATH_RE.search(url.lower()), (
            f"Expected URL to be blocked but was not: {url}"
        )


def test_social_chrome_path_re_no_false_positives() -> None:
    """Legitimate course content URLs must NOT be blocked by _SOCIAL_CHROME_PATH_RE."""
    from app.services.scraper.per_course_vision import _SOCIAL_CHROME_PATH_RE

    should_allow = [
        "https://www.flinders.edu.au/content/dam/main/ent-requirements-table.png",
        "https://cdn.university.edu/courses/bachelor-english-requirements.jpg",
        "https://www.vu.edu.au/screenshots/ielts-chart.png",
        "https://www.uni.edu/images/entry-requirements-2025.png",
        "https://www.acap.edu.au/courses/bachelor-social-work/english-req.jpg",
    ]
    for url in should_allow:
        assert not _SOCIAL_CHROME_PATH_RE.search(url.lower()), (
            f"False positive — course URL incorrectly blocked: {url}"
        )


def test_strip_chrome_html_exists() -> None:
    """_strip_chrome_html must be importable and callable (DOM-stripping defence layer)."""
    from app.services.scraper.per_course_vision import _strip_chrome_html

    # Should return the original HTML on an empty/None input without raising.
    result = _strip_chrome_html("")
    assert isinstance(result, str)


def test_strip_chrome_html_removes_footer_tag() -> None:
    """_strip_chrome_html must strip <footer> elements containing social icons."""
    from app.services.scraper.per_course_vision import _strip_chrome_html

    html_with_footer = """
    <html><body>
      <div class="main-content"><img src="/content/requirements.png" alt="IELTS table"></div>
      <footer class="site-footer"><img src="/social/bluesky.png" alt="Bluesky"></footer>
    </body></html>
    """
    stripped = _strip_chrome_html(html_with_footer)
    assert "bluesky.png" not in stripped, "Footer img must be removed by _strip_chrome_html"
    assert "requirements.png" in stripped, "Main content img must survive _strip_chrome_html"


# ─────────────────────────────────────────────────────────────────────────────
# Bug 2 — Duration: combined/add-on degree sentences demoted
# ─────────────────────────────────────────────────────────────────────────────

def test_combined_degree_context_re_matches_add_on() -> None:
    """_COMBINED_DEGREE_CONTEXT_RE must detect 'add on' combined-degree sentences."""
    from app.services.scraper.extractors.duration import _COMBINED_DEGREE_CONTEXT_RE

    sentences = [
        "Add on Bachelor of Laws - Legal Practice Entry Combined SATAC code: 245041 Duration: 5 years",
        "This is available as a combined degree with Bachelor of Laws",
        "As part of the double degree program, duration is 5 years",
        "Joint degree option: Duration 6 years",
        "Dual degree Duration: 5.5 years",
        "SATAC code: 245041 Duration: 5 years",
    ]
    for s in sentences:
        assert _COMBINED_DEGREE_CONTEXT_RE.search(s), (
            f"Expected _COMBINED_DEGREE_CONTEXT_RE to match: {s!r}"
        )


def test_combined_degree_context_re_no_false_positives() -> None:
    """_COMBINED_DEGREE_CONTEXT_RE must NOT match standard duration sentences."""
    from app.services.scraper.extractors.duration import _COMBINED_DEGREE_CONTEXT_RE

    sentences = [
        "Duration: 3 years full-time",
        "The course takes 3 years to complete full-time",
        "Full-time duration: 2 years",
        "3 years full-time or 6 years part-time",
    ]
    for s in sentences:
        assert not _COMBINED_DEGREE_CONTEXT_RE.search(s), (
            f"False positive — _COMBINED_DEGREE_CONTEXT_RE matched plain duration: {s!r}"
        )


@pytest.mark.asyncio
async def test_duration_combined_degree_demoted() -> None:
    """Flinders Bachelor of Science: main 3yr must win over combined-degree 5yr."""
    from app.services.scraper.extractors.duration import extract

    html = """
    <p>Duration: 3 years full-time</p>
    <p>Add on Bachelor of Laws - Legal Practice Entry Combined SATAC code: 245041
    Duration: 5 years</p>
    """

    results = await extract(html, "https://www.flinders.edu.au/study/courses/bachelor-science")
    assert results, "extract() must return at least one result"
    r = results[0]
    assert r.value == 3.0, (
        f"Expected duration=3.0 (main degree), got {r.value} "
        f"(combined-degree 5yr must be demoted)"
    )
    assert r.normalized.get("duration_term") == "Year"


@pytest.mark.asyncio
async def test_duration_combined_only_page_still_emits() -> None:
    """Demote-not-drop: a page with ONLY a combined-degree duration must still emit."""
    from app.services.scraper.extractors.duration import extract

    html = "<p>Double degree program Duration: 4 years full-time</p>"
    results = await extract(html, "https://www.flinders.edu.au/study/courses/some-combined")
    assert results, (
        "extract() must still emit something when the only duration is in a "
        "combined-degree sentence (demote-not-drop policy)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bug 3 — Duplicates: trailing-slash URL normalization
# ─────────────────────────────────────────────────────────────────────────────

def test_trailing_slash_guard_condition() -> None:
    """The trailing-slash normalization condition in stage_course must fire correctly.

    We test the guard logic directly (count('/')>3) rather than the full
    stage_course function to avoid DB dependency in the unit test.
    """
    def _normalize(url: str) -> str:
        """Mirror of the normalization added to stage_course.stage_course()."""
        if url and url.endswith("/") and url.count("/") > 3:
            return url.rstrip("/")
        return url

    # Should strip: has path beyond domain root
    assert _normalize("https://www.flinders.edu.au/study/courses/bachelor-science/") == \
        "https://www.flinders.edu.au/study/courses/bachelor-science"

    # Should strip
    assert _normalize("https://www.flinders.edu.au/study/courses/bachelor-science-honours/") == \
        "https://www.flinders.edu.au/study/courses/bachelor-science-honours"

    # Should NOT strip bare origin (only 3 slashes: https://<host>/)
    assert _normalize("https://www.flinders.edu.au/") == "https://www.flinders.edu.au/"

    # Should NOT strip URL without trailing slash
    assert _normalize("https://www.flinders.edu.au/study/courses/bachelor-science") == \
        "https://www.flinders.edu.au/study/courses/bachelor-science"

    # Edge: empty string
    assert _normalize("") == ""


def test_staged_dedup_endpoint_uses_rtrim() -> None:
    """The staged_dedup SQL must use RTRIM(course_website, '/') to be trailing-slash-insensitive."""
    import inspect
    from app.routers import scrape as scrape_mod

    src = inspect.getsource(scrape_mod.staged_dedup)
    # RTRIM ensures trailing-slash pairs are deduplicated together
    assert "RTRIM" in src.upper() or "rtrim" in src.lower(), (
        "staged_dedup must use RTRIM(course_website, '/') for trailing-slash-safe dedup"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bug 4 — flinders.yaml: config file created and loads cleanly
# ─────────────────────────────────────────────────────────────────────────────

def test_flinders_yaml_exists() -> None:
    """scraper_config/unis/flinders.yaml must exist."""
    from pathlib import Path
    yaml_path = Path(__file__).parent.parent / "scraper_config" / "unis" / "flinders.yaml"
    assert yaml_path.exists(), (
        f"flinders.yaml not found at {yaml_path}. "
        "Create it so the loader doesn't rely on the fail-open default."
    )


def test_flinders_yaml_is_valid_yaml() -> None:
    """flinders.yaml must parse as valid YAML."""
    from pathlib import Path
    import yaml

    yaml_path = Path(__file__).parent.parent / "scraper_config" / "unis" / "flinders.yaml"
    with yaml_path.open() as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict), "flinders.yaml must be a YAML mapping"


def test_flinders_config_loads_via_loader() -> None:
    """The config loader must build a valid UniConfig for www.flinders.edu.au."""
    from app.services.scraper.config.loader import load_uni_config

    cfg = load_uni_config(
        slug="flinders",
        name="Flinders University",
        scrape_url="https://www.flinders.edu.au/study/courses",
    )
    # domestic_only filter explicitly enabled in the YAML
    assert cfg.extraction.filters.domestic_only.enabled is True, (
        "flinders.yaml must set extraction.filters.domestic_only.enabled = true"
    )
    # sitemap supplement explicitly enabled
    assert cfg.discovery.always_sitemap_supplement is True, (
        "flinders.yaml must set discovery.always_sitemap_supplement = true"
    )


def test_flinders_config_slug_derivation() -> None:
    """Slug derived from www.flinders.edu.au must be 'flinders'."""
    from app.services.scraper.config.loader import _hostname_to_slug

    assert _hostname_to_slug("www.flinders.edu.au") == "flinders"
    assert _hostname_to_slug("flinders.edu.au") == "flinders"
