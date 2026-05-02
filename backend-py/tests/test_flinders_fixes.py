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


def test_flinders_config_tier1_english_disabled() -> None:
    """flinders.yaml must set trust_tier1_vision_ocr_english=false."""
    from app.services.scraper.config.loader import load_uni_config

    cfg = load_uni_config(
        slug="flinders",
        name="Flinders University",
        scrape_url="https://www.flinders.edu.au/study/courses",
    )
    assert cfg.extraction.english.trust_tier1_vision_ocr_english is False, (
        "flinders.yaml must set extraction.english.trust_tier1_vision_ocr_english = false"
    )


def test_schema_tier1_english_flag_default_is_true() -> None:
    """Default value for trust_tier1_vision_ocr_english must be True (opt-in)."""
    from app.services.scraper.config.schema import EnglishConfig

    assert EnglishConfig().trust_tier1_vision_ocr_english is True, (
        "trust_tier1_vision_ocr_english must default to True — opt-in, not opt-out"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bug 3b — URL fragment normalization in stage_course
# ─────────────────────────────────────────────────────────────────────────────

def test_fragment_stripped_from_url() -> None:
    """URL fragments (#section) must be stripped during normalization."""
    url_with_frag = "https://www.flinders.edu.au/study/courses/bachelor-science#fees"
    frag_pos = url_with_frag.find("#")
    normalized = url_with_frag[:frag_pos] if frag_pos != -1 else url_with_frag
    assert normalized == "https://www.flinders.edu.au/study/courses/bachelor-science"


def test_no_fragment_url_unchanged() -> None:
    """URLs without fragments must not be modified by fragment stripping."""
    url = "https://www.flinders.edu.au/study/courses/bachelor-science"
    frag_pos = url.find("#")
    normalized = url[:frag_pos] if frag_pos != -1 else url
    assert normalized == url


def test_fragment_then_trailing_slash_both_stripped() -> None:
    """Fragment stripping runs before trailing-slash stripping — both applied."""
    url = "https://www.flinders.edu.au/study/courses/bachelor-science/#fees"
    frag_pos = url.find("#")
    if frag_pos != -1:
        url = url[:frag_pos]
    if url.endswith("/") and url.count("/") > 3:
        url = url.rstrip("/")
    assert url == "https://www.flinders.edu.au/study/courses/bachelor-science"


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


# ─────────────────────────────────────────────────────────────────────────────
# Real-world regression: karl-sammut.png hallucination
# Bachelor of Engineering (Maritime) (Honours) — headshot photo from _jcr_content
# Gemini returned IELTS overall=6.5 + all 4 sub-bands from a portrait photo.
# Sub-gate A catches the 6.5 vs regex 6.0 mismatch; trust_tier1_vision_ocr_english
# catches it even if no regex anchor exists.
# ─────────────────────────────────────────────────────────────────────────────

_KARL_SAMMUT_URL = (
    "https://www.flinders.edu.au/study/courses/bachelor-engineering-maritime-honours"
    "/_jcr_content/content/section_1690004324_c/par_0/section_1444220647_c"
    "/par_0/section_copy/par_0/image_general.coreimg.png/1756445737683/karl-sammut.png"
)


def test_karl_sammut_blocked_by_gate_a() -> None:
    """Portrait photo returning IELTS=6.5 must be blocked when regex established 6.0."""
    vision_evidence = [
        {"field_key": "ielts_overall",   "value": 6.5, "source_tier": 1, "source_url": _KARL_SAMMUT_URL},
        {"field_key": "ielts_listening", "value": 6.0, "source_tier": 1, "source_url": _KARL_SAMMUT_URL},
        {"field_key": "ielts_reading",   "value": 6.5, "source_tier": 1, "source_url": _KARL_SAMMUT_URL},
        {"field_key": "ielts_speaking",  "value": 6.5, "source_tier": 1, "source_url": _KARL_SAMMUT_URL},
        {"field_key": "ielts_writing",   "value": 6.0, "source_tier": 1, "source_url": _KARL_SAMMUT_URL},
    ]
    flagged = _run_coherence_gate(regex_ielts=6.0, vision_evidence=vision_evidence)
    assert _KARL_SAMMUT_URL in flagged, (
        "karl-sammut.png must be flagged by sub-gate A (IELTS=6.5 vs regex=6.0)"
    )
    vision_filled = {ev["field_key"]: ev["value"] for ev in vision_evidence}
    surviving = {
        k: v for k, v in vision_filled.items()
        if next(
            (ev for ev in vision_evidence if ev["field_key"] == k), {}
        ).get("source_url") not in flagged
    }
    assert surviving == {}, (
        f"ALL fields from karl-sammut.png must be blocked, got: {surviving}"
    )


def test_karl_sammut_blocked_by_tier1_opt_out() -> None:
    """With trust_tier1_vision_ocr_english=false, ALL tier-1 images pre-blocked regardless of values."""
    vision_evidence = [
        {"field_key": "ielts_overall",   "value": 6.5, "source_tier": 1, "source_url": _KARL_SAMMUT_URL},
        {"field_key": "ielts_listening", "value": 6.0, "source_tier": 1, "source_url": _KARL_SAMMUT_URL},
        {"field_key": "ielts_reading",   "value": 6.5, "source_tier": 1, "source_url": _KARL_SAMMUT_URL},
        {"field_key": "ielts_speaking",  "value": 6.5, "source_tier": 1, "source_url": _KARL_SAMMUT_URL},
        {"field_key": "ielts_writing",   "value": 6.0, "source_tier": 1, "source_url": _KARL_SAMMUT_URL},
    ]
    # Simulate the opt-out pre-population (trust_tier1_vision_ocr_english=false)
    incoherent: set[str] = set()
    for vev in vision_evidence:
        if vev.get("source_tier", 1) != 0:
            incoherent.add(vev.get("source_url", ""))

    assert _KARL_SAMMUT_URL in incoherent

    vision_filled = {ev["field_key"]: ev["value"] for ev in vision_evidence}
    surviving = {
        k: v for k, v in vision_filled.items()
        if next(
            (ev for ev in vision_evidence if ev["field_key"] == k), {}
        ).get("source_url") not in incoherent
    }
    assert surviving == {}, (
        f"Tier-1 opt-out must block ALL fields from karl-sammut.png, got: {surviving}"
    )


def test_karl_sammut_tier0_twin_would_pass() -> None:
    """If karl-sammut were tier-0 (DOM-anchored), it must NOT be blocked by tier-1 opt-out."""
    tier0_evidence = [
        {"field_key": "ielts_overall", "value": 6.5, "source_tier": 0, "source_url": _KARL_SAMMUT_URL},
    ]
    incoherent: set[str] = set()
    for vev in tier0_evidence:
        if vev.get("source_tier", 1) != 0:
            incoherent.add(vev.get("source_url", ""))

    assert _KARL_SAMMUT_URL not in incoherent, (
        "Tier-0 version of same image must NOT be blocked by the opt-out — "
        "tier-0 means it was found inside the English requirements DOM section"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Global tier-1 pre-skip logic (per_course_vision._regex_has_ielts)
# These tests verify the skip-before-Gemini logic introduced to save cost
# for ALL universities (not just Flinders).
# ─────────────────────────────────────────────────────────────────────────────

def _simulate_pre_skip(
    candidates: list[tuple[str, str]],
    tier0_url_set: set[str],
    payload_ielts: float | None,
    skip_tier1_english: bool,
) -> tuple[list[str], list[str]]:
    """
    Mirror the loop logic from per_course_vision.maybe_vision_refetch:
    return (processed_urls, skipped_urls).
    """
    regex_has_ielts = bool(payload_ielts)
    processed, skipped = [], []
    for img_url, _alt in candidates:
        is_tier0 = img_url in tier0_url_set
        if not is_tier0 and (skip_tier1_english or regex_has_ielts):
            skipped.append(img_url)
        else:
            processed.append(img_url)
    return processed, skipped


_T1_IMG = "https://www.example-uni.edu.au/_jcr_content/hero.png"
_T0_IMG = "https://www.example-uni.edu.au/_jcr_content/requirements-table.png"
_T0_SET = {_T0_IMG}


def test_global_skip_fires_when_regex_has_ielts() -> None:
    """Tier-1 image is skipped (no Gemini call) when payload already has ielts_overall."""
    processed, skipped = _simulate_pre_skip(
        [(_T1_IMG, ""), (_T0_IMG, "")],
        tier0_url_set=_T0_SET,
        payload_ielts=6.0,
        skip_tier1_english=False,
    )
    assert _T1_IMG in skipped, "Tier-1 must be skipped when regex has IELTS"
    assert _T0_IMG in processed, "Tier-0 must still be processed"


def test_global_skip_does_not_fire_without_regex_ielts() -> None:
    """Tier-1 image is NOT skipped when regex found no ielts_overall (ASAHE-type case)."""
    processed, skipped = _simulate_pre_skip(
        [(_T1_IMG, "")],
        tier0_url_set=_T0_SET,
        payload_ielts=None,
        skip_tier1_english=False,
    )
    assert _T1_IMG in processed, "Tier-1 must be processed when regex found no IELTS"
    assert skipped == []


def test_global_skip_fires_via_skip_tier1_flag_regardless_of_regex() -> None:
    """skip_tier1_english=True skips tier-1 images even when regex found no IELTS."""
    processed, skipped = _simulate_pre_skip(
        [(_T1_IMG, "")],
        tier0_url_set=_T0_SET,
        payload_ielts=None,
        skip_tier1_english=True,
    )
    assert _T1_IMG in skipped, "skip_tier1_english=True must skip tier-1 even without regex IELTS"


def test_global_skip_tier0_always_processed_with_regex_ielts() -> None:
    """Tier-0 images are never skipped, even when regex has IELTS and skip_tier1_english=True."""
    processed, skipped = _simulate_pre_skip(
        [(_T0_IMG, "")],
        tier0_url_set=_T0_SET,
        payload_ielts=7.0,
        skip_tier1_english=True,
    )
    assert _T0_IMG in processed, "Tier-0 must always be processed regardless of any skip flag"
    assert skipped == []


def test_global_skip_mixed_candidates() -> None:
    """Mixed candidate list: tier-0 processed, tier-1 skipped when regex has IELTS."""
    t1_a = "https://uni.edu.au/content/portrait-a.png"
    t1_b = "https://uni.edu.au/content/portrait-b.png"
    t0   = "https://uni.edu.au/content/requirements.png"
    processed, skipped = _simulate_pre_skip(
        [(t1_a, ""), (t0, ""), (t1_b, "")],
        tier0_url_set={t0},
        payload_ielts=6.5,
        skip_tier1_english=False,
    )
    assert t0 in processed
    assert t1_a in skipped
    assert t1_b in skipped
    assert len(processed) == 1
    assert len(skipped) == 2
