"""Tests for QMUL course-count regression fix (Task #252).

Root cause: QMUL pages contain "online" in navigation/utility text
("Apply Online", "Online Learning", "Online Prospectus"). The bare
\bonline\b study_mode fallback (confidence 0.5) fired on that nav copy
and set study_mode="Online", triggering the online_only filter and silently
rejecting on-campus courses.

Fix: online_only_requires_strong_evidence=true suppresses the bare keyword
result; bfs_page_budget raised to 100; expected_min_courses: 370 guard added.

Covers:
  1. YAML: bfs_page_budget is 100 (raised from 60)
  2. YAML: expected_min_courses is 370
  3. YAML: study_mode.online_only_requires_strong_evidence is True
  4. YAML: study_mode.prefer_location_over_online_keyword is True
  5. YAML: online_only filter remains enabled (for genuine distance-learning)
  6. study_mode extractor suppresses bare "online" for QMUL config
  7. study_mode extractor keeps strong phrases ("fully online", "100% online",
     "distance learning") even with requires_strong_evidence=True
  8. study_mode extractor keeps "On Campus" classification unaffected
"""
from __future__ import annotations

import pytest

from app.services.scraper.config.context import set_uni_config
from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.config.schema import (
    ExtractionConfig,
    StudyModeConfig,
    UniConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_QMUL_URL = "https://www.qmul.ac.uk/undergraduate/coursefinder/courses/computer-science/"
_QMUL_SCRAPE_URL = "https://www.qmul.ac.uk/study/undergraduate/"


def _qmul_cfg():
    return load_uni_config(
        slug="qmul",
        name="Queen Mary University of London",
        scrape_url=_QMUL_SCRAPE_URL,
    )


def _uni_config_with_study_mode(**study_mode_kwargs) -> UniConfig:
    sm = StudyModeConfig(**study_mode_kwargs)
    extr = ExtractionConfig(study_mode=sm)
    return UniConfig(
        slug="qmul",
        name="Queen Mary University of London",
        base_url="https://www.qmul.ac.uk",
        scrape_url=_QMUL_SCRAPE_URL,
        extraction=extr,
    )


# ---------------------------------------------------------------------------
# 1–5: YAML value assertions (sync — no extractor calls)
# ---------------------------------------------------------------------------

def test_qmul_bfs_budget_is_100():
    """bfs_page_budget is intentionally unset (None) since QMUL moved to
    searchstax links_only discovery: the Solr core supplies all course URLs
    directly, so BFS never runs and the page-budget knob is superseded.
    """
    cfg = _qmul_cfg()
    assert cfg.discovery.bfs_page_budget is None, (
        f"Expected None (BFS superseded by searchstax links_only), "
        f"got {cfg.discovery.bfs_page_budget}"
    )


def test_qmul_expected_min_courses_guard():
    """expected_min_courses must be 390 (5% below the 409-course Solr baseline,
    updated from the earlier 396-course/370 guard once discovery moved to
    querying the SearchStax Solr core directly).
    """
    cfg = _qmul_cfg()
    assert cfg.discovery.expected_min_courses == 390, (
        f"Expected 390, got {cfg.discovery.expected_min_courses}"
    )


def test_qmul_study_mode_online_requires_strong_evidence():
    """online_only_requires_strong_evidence must be True for QMUL."""
    cfg = _qmul_cfg()
    sm = cfg.extraction.study_mode
    assert sm is not None, "extraction.study_mode is not set"
    assert sm.online_only_requires_strong_evidence is True, (
        f"Expected True, got {sm.online_only_requires_strong_evidence}. "
        "This suppresses bare 'online' keyword (confidence 0.5) from "
        "QMUL's nav text ('Apply Online', 'Online Learning' links)."
    )


def test_qmul_study_mode_prefer_location_over_online():
    """prefer_location_over_online_keyword must be True for QMUL."""
    cfg = _qmul_cfg()
    sm = cfg.extraction.study_mode
    assert sm is not None
    assert sm.prefer_location_over_online_keyword is True, (
        f"Expected True, got {sm.prefer_location_over_online_keyword}"
    )


def test_qmul_online_only_filter_remains_enabled():
    """online_only filter must remain enabled (for genuine distance-learning)."""
    cfg = _qmul_cfg()
    flt = cfg.extraction.filters.online_only
    assert flt.enabled is True, (
        f"Expected enabled=True, got {flt.enabled}. "
        "QMUL has some distance-learning programmes that should be rejected."
    )


# ---------------------------------------------------------------------------
# 6–8: study_mode extractor behaviour with QMUL config
#
# Uses @pytest.mark.asyncio — same pattern as test_browser_rescue_skip.py.
# set_uni_config() wires the ContextVar; asyncio.Mode.STRICT keeps tests
# isolated (each test gets its own event loop scope).
# ---------------------------------------------------------------------------

from app.services.scraper.extractors import study_mode as _sm_extractor


@pytest.mark.asyncio
async def test_bare_online_keyword_suppressed_with_strong_evidence_config():
    """Bare 'online' in nav text must produce no result when
    online_only_requires_strong_evidence=True.

    Simulates a QMUL on-campus course page that contains "Apply Online"
    and "Online Learning" in its navigation — text that should NOT classify
    the course as Online.  The <nav> block is already stripped by
    _NOISE_BLOCK_RE, but non-<nav> "Apply Online" buttons in the main body
    would not be.  This test uses inline body text to verify the flag works
    even when nav stripping doesn't help.
    """
    html = """
    <html><body>
    <h1>BSc Computer Science</h1>
    <p>Apply online for this course at Mile End campus.</p>
    <p>Our online prospectus is available for download.</p>
    <div class="course-facts">
      <dt>Location</dt><dd>Mile End, London</dd>
      <dt>Duration</dt><dd>3 years</dd>
    </div>
    </body></html>
    """
    set_uni_config(_uni_config_with_study_mode(online_only_requires_strong_evidence=True))
    results = await _sm_extractor.extract(html, _QMUL_URL)
    online_results = [r for r in results if r.value == "Online"]
    assert not online_results, (
        f"Expected no Online result, got: {[r.value for r in results]}. "
        "Body 'apply online' / 'online prospectus' should NOT classify "
        "the course as Online when online_only_requires_strong_evidence=True."
    )


@pytest.mark.asyncio
async def test_strong_online_phrases_detected_with_strong_evidence_config():
    """'Fully online' / '100% online' / 'distance learning' must still be
    detected even when online_only_requires_strong_evidence=True.

    These high-specificity phrases identify genuine distance-learning
    programmes that should continue to be rejected by the online_only filter.
    """
    for phrase in ("fully online", "100% online", "distance learning"):
        html = f"""
        <html><body>
        <h1>Graduate Certificate in Business Administration</h1>
        <p>This programme is {phrase} and requires no campus attendance.</p>
        </body></html>
        """
        set_uni_config(_uni_config_with_study_mode(online_only_requires_strong_evidence=True))
        results = await _sm_extractor.extract(html, _QMUL_URL)
        modes = [r.value for r in results]
        assert "Online" in modes, (
            f"Expected Online for '{phrase}' (high-specificity phrase), got: {modes}. "
            "Strong phrases must survive requires_strong_evidence filtering."
        )


@pytest.mark.asyncio
async def test_on_campus_label_unaffected_by_strong_evidence_config():
    """Structured 'Mode of study: On Campus' label must win over incidental
    'apply online' text even when online_only_requires_strong_evidence=True.

    The flag only suppresses the bare-keyword Online fallback (confidence 0.5).
    Structured label detection is unaffected.
    """
    html = """
    <html><body>
    <h1>LLB Law</h1>
    <p>Mode of study: On Campus</p>
    <p>Apply online by January 15.</p>
    </body></html>
    """
    set_uni_config(_uni_config_with_study_mode(online_only_requires_strong_evidence=True))
    results = await _sm_extractor.extract(html, _QMUL_URL)
    modes = [r.value for r in results]
    assert "On Campus" in modes, (
        f"Expected 'On Campus' from structured label, got: {modes}"
    )
    assert "Online" not in modes, (
        f"'Online' must not appear when On Campus label is authoritative, got: {modes}"
    )
