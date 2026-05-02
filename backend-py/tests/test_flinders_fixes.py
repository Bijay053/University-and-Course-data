"""Tests for Flinders-specific bugs fixed in this session:

  Bug 1 — Vision OCR: social-footer icons blocked by _SOCIAL_CHROME_PATH_RE
  Bug 2 — Duration: combined/add-on degree sentences demoted by 0.001×
  Bug 3 — Duplicates: trailing-slash URL normalization in stage_course
  Bug 4 — flinders.yaml: config file created and loads cleanly
  Bug 5 — Vision IELTS coherence gate: tier-1 images with mismatched IELTS
           must not silently inject TOEFL/PTE into empty slots
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


# ─────────────────────────────────────────────────────────────────────────────
# Bug 5 — Tier-1 IELTS coherence gate in single_course pipeline
# ─────────────────────────────────────────────────────────────────────────────

def _run_coherence_gate(
    regex_ielts: float | None,
    vision_evidence: list[dict],
) -> set[str]:
    """Replicate the incoherent-image-URL detection logic from single_course.py."""
    incoherent: set[str] = set()
    if regex_ielts is None:
        return incoherent
    for vev in vision_evidence:
        if (
            vev.get("field_key") == "ielts_overall"
            and vev.get("source_tier", 1) != 0
        ):
            try:
                if abs(float(vev["value"]) - regex_ielts) > 0.1:
                    incoherent.add(vev.get("source_url", ""))
            except (TypeError, ValueError):
                pass
    return incoherent


def test_coherence_gate_flags_mismatched_tier1_image() -> None:
    """Tier-1 image returning IELTS=6.5 when regex said 6.0 must be flagged."""
    evidence = [
        {
            "field_key": "ielts_overall",
            "value": 6.5,
            "source_tier": 1,
            "source_url": "https://flinders.edu.au/_jcr_content/hero.jpg",
        }
    ]
    flagged = _run_coherence_gate(regex_ielts=6.0, vision_evidence=evidence)
    assert "https://flinders.edu.au/_jcr_content/hero.jpg" in flagged, (
        "Image with IELTS=6.5 should be flagged when regex established 6.0"
    )


def test_coherence_gate_passes_matching_tier1_image() -> None:
    """Tier-1 image returning same IELTS as regex must NOT be flagged."""
    evidence = [
        {
            "field_key": "ielts_overall",
            "value": 6.0,
            "source_tier": 1,
            "source_url": "https://flinders.edu.au/_jcr_content/reqs.jpg",
        }
    ]
    flagged = _run_coherence_gate(regex_ielts=6.0, vision_evidence=evidence)
    assert not flagged, "Image matching regex IELTS should not be flagged"


def test_coherence_gate_never_flags_tier0_image() -> None:
    """Tier-0 (English-requirements DOM) image must never be flagged even if IELTS differs."""
    evidence = [
        {
            "field_key": "ielts_overall",
            "value": 7.0,
            "source_tier": 0,
            "source_url": "https://flinders.edu.au/_jcr_content/reqs-table.png",
        }
    ]
    flagged = _run_coherence_gate(regex_ielts=6.0, vision_evidence=evidence)
    assert not flagged, "Tier-0 images must never be flagged by the coherence gate"


def test_coherence_gate_noop_when_no_regex_ielts() -> None:
    """Gate must be a no-op when regex didn't find any IELTS value."""
    evidence = [
        {
            "field_key": "ielts_overall",
            "value": 6.5,
            "source_tier": 1,
            "source_url": "https://flinders.edu.au/_jcr_content/banner.jpg",
        }
    ]
    flagged = _run_coherence_gate(regex_ielts=None, vision_evidence=evidence)
    assert not flagged, "Gate must be a no-op when regex_ielts is None"


def test_coherence_gate_blocks_toefl_injection() -> None:
    """Flagged image's TOEFL/PTE must be excluded — simulates the bachelor-arts case."""
    img_url = "https://flinders.edu.au/_jcr_content/hero.png"
    vision_evidence = [
        {"field_key": "ielts_overall", "value": 6.5, "source_tier": 1, "source_url": img_url},
        {"field_key": "toefl_overall", "value": 80.0, "source_tier": 1, "source_url": img_url},
        {"field_key": "pte_overall",   "value": 58.0, "source_tier": 1, "source_url": img_url},
    ]
    vision_filled = {"ielts_overall": 6.5, "toefl_overall": 80.0, "pte_overall": 58.0}

    flagged = _run_coherence_gate(regex_ielts=6.0, vision_evidence=vision_evidence)
    assert img_url in flagged

    surviving = {
        k: v for k, v in vision_filled.items()
        if next(
            (ev for ev in vision_evidence if ev["field_key"] == k), {}
        ).get("source_url") not in flagged
    }
    assert surviving == {}, (
        "All fields from the incoherent image must be blocked; "
        f"surviving fields: {surviving}"
    )


def test_coherence_gate_allows_toefl_from_coherent_image() -> None:
    """TOEFL from a tier-1 image that returns matching IELTS must pass through."""
    img_url = "https://flinders.edu.au/_jcr_content/reqs.png"
    vision_evidence = [
        {"field_key": "ielts_overall", "value": 6.0, "source_tier": 1, "source_url": img_url},
        {"field_key": "toefl_overall", "value": 79.0, "source_tier": 1, "source_url": img_url},
    ]
    vision_filled = {"ielts_overall": 6.0, "toefl_overall": 79.0}

    flagged = _run_coherence_gate(regex_ielts=6.0, vision_evidence=vision_evidence)
    assert not flagged

    surviving = {
        k: v for k, v in vision_filled.items()
        if next(
            (ev for ev in vision_evidence if ev["field_key"] == k), {}
        ).get("source_url") not in flagged
    }
    assert surviving == {"ielts_overall": 6.0, "toefl_overall": 79.0}, (
        "Coherent image should not be blocked; TOEFL should survive"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bug 5b — Sub-gate B: single-test tier-1 image rejected when no regex anchor
# ─────────────────────────────────────────────────────────────────────────────

_ENGLISH_OVERALL_SLOTS = frozenset({
    "ielts_overall", "pte_overall", "toefl_overall",
    "cambridge_overall", "duolingo_overall",
})


def _run_single_test_gate(vision_evidence: list[dict]) -> set[str]:
    """Replicate sub-gate B (no regex anchor) logic from single_course.py."""
    from collections import Counter
    t1_overall_count: dict[str, int] = Counter(
        ev["source_url"]
        for ev in vision_evidence
        if (
            ev.get("source_tier", 1) != 0
            and ev.get("field_key") in _ENGLISH_OVERALL_SLOTS
            and ev.get("source_url")
        )
    )
    return {url for url, cnt in t1_overall_count.items() if cnt < 2}


def test_single_test_gate_rejects_ielts_only_image() -> None:
    """Tier-1 image with only IELTS (no TOEFL/PTE) must be flagged when regex IELTS is None."""
    img_url = "https://flinders.edu.au/_jcr_content/hero-ielts-caption.png"
    evidence = [
        {"field_key": "ielts_overall", "value": 6.5, "source_tier": 1, "source_url": img_url},
    ]
    flagged = _run_single_test_gate(evidence)
    assert img_url in flagged, (
        "Single-test tier-1 image (IELTS only) must be flagged without regex anchor"
    )


def test_single_test_gate_passes_multi_test_image() -> None:
    """Tier-1 image with IELTS + TOEFL must NOT be flagged — looks like a real requirements table."""
    img_url = "https://flinders.edu.au/_jcr_content/requirements-table.png"
    evidence = [
        {"field_key": "ielts_overall", "value": 6.0, "source_tier": 1, "source_url": img_url},
        {"field_key": "toefl_overall", "value": 79.0, "source_tier": 1, "source_url": img_url},
    ]
    flagged = _run_single_test_gate(evidence)
    assert not flagged, (
        "Multi-test tier-1 image (IELTS + TOEFL) should NOT be flagged"
    )


def test_single_test_gate_never_flags_tier0_image() -> None:
    """Tier-0 images must never be flagged by sub-gate B even with only one test score."""
    img_url = "https://flinders.edu.au/_jcr_content/dom-anchored-reqs.png"
    evidence = [
        {"field_key": "ielts_overall", "value": 7.0, "source_tier": 0, "source_url": img_url},
    ]
    flagged = _run_single_test_gate(evidence)
    assert not flagged, "Tier-0 image must never be flagged by sub-gate B"


def test_single_test_gate_three_test_image_passes() -> None:
    """Tier-1 image with IELTS + PTE + TOEFL passes (≥2 overalls)."""
    img_url = "https://flinders.edu.au/_jcr_content/full-table.png"
    evidence = [
        {"field_key": "ielts_overall", "value": 6.0, "source_tier": 1, "source_url": img_url},
        {"field_key": "pte_overall",   "value": 50.0, "source_tier": 1, "source_url": img_url},
        {"field_key": "toefl_overall", "value": 79.0, "source_tier": 1, "source_url": img_url},
    ]
    flagged = _run_single_test_gate(evidence)
    assert not flagged, "Three-test tier-1 image must not be flagged"
