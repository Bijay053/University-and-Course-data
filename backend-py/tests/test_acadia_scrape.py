"""Acadia's subject programme pages must not be dropped or read as all subjects."""
import re

import pytest

from app.services.scraper.config.context import current_uni_config
from app.services.scraper.config.loader import _hostname_to_slug, get_config_for_host
from app.services.scraper.extractors.acadia_html import scope_programme_html
from app.services.scraper.guards import should_stage_course

URL = "https://www2.acadiau.ca/academics/undergraduate/computer-science.html"


def config():
    return get_config_for_host(
        hostname="www2.acadiau.ca", name="Acadia University", scrape_url="https://www2.acadiau.ca",
        university_id=104, create_missing_stub=False,
        db_scrape_config={"admin_config": {"discovery": {
            "seed_urls": [], "allow_url_patterns": [], "block_url_patterns": [],
        }, "extraction": {"skip_browser_rescue": False,
                         "skip_per_course_browser": False}}},
    )


@pytest.mark.parametrize("hostname,slug", [
    ("www2.acadiau.ca", "acadiau"),
    ("WWW2.ACADIAU.CA", "acadiau"),
    ("www1.cbu.ca", "cbu"),
    ("www42.cbu.ca", "cbu"),
    ("www2.study.csu.edu.au", "csu"),
    ("www2academic.cbu.ca", "www2academic"),
    ("www2.ca", "www2"),
    ("courses.hud.ac.uk", "hud"),
])
def test_numbered_www_hosts_use_institution_recipe_without_stripping_real_names(hostname, slug):
    assert _hostname_to_slug(hostname) == slug


def test_subject_named_programme_survives_staging_without_invented_award():
    token = current_uni_config.set(config())
    try:
        accepted, reason = should_stage_course(
            "Computer Science", {"course_name": "Computer Science"},
            source_url=URL,
        )
        assert accepted, reason
    finally:
        current_uni_config.reset(token)


@pytest.mark.parametrize("path,accepted", [
    ("/academics/undergraduate/computer-science.html", True),
    ("/academics/graduate/computer-science.html", True),
    ("/academics/undergraduate/history.html", True),
    ("/academics/undergraduate/honours.html", False),
    ("/academics/undergraduate/honours/usra.html", False),
    ("/academics/graduate/current-students.html", False),
    ("/academics/graduate/forms.html", False),
    ("/academics/graduate/money-matters.html", False),
    ("/academics/graduate/money-matters/other-external-funding.html", False),
    ("/student-services/diploma-frames.html", False),
    ("/admissions/undergraduate.html", False),
])
def test_discovery_boundary(path, accepted):
    cfg = config().discovery
    url = "https://www2.acadiau.ca" + path
    keep = any(re.search(p, url) for p in cfg.course_detail_url_patterns)
    keep = keep and not any(re.search(p, url) for p in cfg.block_url_patterns)
    assert keep is accepted


def test_static_browser_suppression_and_pending_fee_policy():
    cfg = config()
    assert cfg.extraction.skip_browser_rescue
    assert cfg.extraction.skip_per_course_browser
    assert not cfg.extraction.staging.require_international_fee
    assert cfg.extraction.staging.dedup_use_full_url
    assert cfg.extraction.fees.default_currency == "CAD"
    assert cfg.discovery.seed_urls[0] == "https://www2.acadiau.ca/academics.html"
    assert cfg.discovery.expected_min_courses == 35


def test_shared_requirements_cannot_supply_another_courses_scores_or_prices():
    html = """
      <h1>Acadia University - Computer Science</h1>
      <nav><a>Graduate English MA</a></nav>
      <main id="main"><article><h2>Computer Science</h2>
      <p>Bachelor of Computer Science and Master of Science routes.</p>
      <p>Duration: 4 years.</p></article>
      <div id="requirements-fees"><h2>Requirements for Nursing</h2>
      <p>IELTS 7.0, tuition CAD 26190.</p></div>
      <div class="modal"><p>Another subject IELTS 6.5</p></div></main>
    """
    scoped = scope_programme_html(html, URL)
    assert "Computer Science" in scoped and "4 years" in scoped
    assert "Acadia University -" not in scoped
    assert "26190" not in scoped and "IELTS" not in scoped
    assert "Graduate English MA" not in scoped
    from bs4 import BeautifulSoup
    again = BeautifulSoup(scope_programme_html(scoped, URL), "html.parser")
    assert again.get_text(" ", strip=True) == BeautifulSoup(
        scoped, "html.parser"
    ).get_text(" ", strip=True)
    assert len(again.select("h1")) == 1
    assert scope_programme_html(html, URL.replace("www2.acadiau.ca", "other.edu")) == html


@pytest.mark.asyncio
async def test_normal_pipeline_extracts_owned_fields_without_browser_refetch(monkeypatch):
    from app.services.scraper.pipelines import single_course
    from app.services.scraper.extractors import gemini_primary

    async def no_primary(*args, **kwargs):
        return {}, 0.0, 0, 0, {"skipped": True}

    async def no_network(*args, **kwargs):
        raise AssertionError("Static Acadia extraction must not refetch")

    monkeypatch.setattr(gemini_primary, "extract_primary", no_primary)
    monkeypatch.setattr(single_course, "fetch_html", no_network)
    monkeypatch.setattr("app.services.scraper.browser_pool.pool.fetch_html", no_network)
    html = """
      <h1>Acadia University - Computer Science</h1>
      <main id="main"><article><h2>Computer Science</h2>
      <p>Duration: 4 years full-time. Bachelor of Computer Science.</p>
      <p>Small classes and project-based learning.</p>
      <div id="requirements-fees"><p>Requirements for Nursing:
      IELTS 7.0, International tuition CAD 26190 per year.</p></div>
      </article></main>
    """
    token = current_uni_config.set(config())
    try:
        result = await single_course.extract_course(
            URL, html=html, country="Canada", use_ai_fallback=False,
        )
        payload = result["payload"]
        assert payload["course_name"] == "Computer Science"
        assert payload["duration"] == 4
        assert payload["duration_term"] == "Year"
        assert payload.get("ielts_overall") is None
        assert payload.get("international_fee") is None
        accepted, reason = should_stage_course(
            payload["course_name"], payload, source_url=URL,
        )
        assert accepted, reason
    finally:
        current_uni_config.reset(token)
