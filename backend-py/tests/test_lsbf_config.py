import re

import pytest

from app.services.scraper import discovery as discovery_mod
from app.services.scraper import sitemap as sitemap_mod
from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.orchestrator import _link_matches_post_discovery_allow


def _load_lsbf(db_scrape_config=None):
    return load_uni_config(
        slug="lsbf",
        scrape_url="https://lsbf.edu.my",
        university_id=50,
        name="London School of Business and Finance Malaysia Campus",
        db_scrape_config=db_scrape_config,
    )


def test_lsbf_tracked_recipe_replaces_stale_generated_courses_path():
    cfg = _load_lsbf(
        {
            "auto_config": {
                "_auto_generated": True,
                "_strategy": "wayback",
                "discovery": {
                    "allow_url_patterns": ["/courses/[^/]+/"],
                    "use_wayback": True,
                },
            }
        }
    )

    assert cfg.discovery.seed_urls == ["https://lsbf.edu.my/programmes/"]
    assert cfg.discovery.use_wayback is False
    assert cfg.discovery.allow_url_patterns == [
        r"^https?://(?:www\.)?lsbf\.edu\.my/programmes/[^/?#]+/?(?:[?#].*)?$"
    ]
    assert cfg.discovery.course_detail_url_patterns == (
        cfg.discovery.allow_url_patterns
    )
    assert cfg.discovery.force_candidate_url_patterns == (
        cfg.discovery.allow_url_patterns
    )
    assert cfg.extraction.staging.skip_degree_qualifier_check is True


def test_lsbf_live_programme_details_survive_but_category_hubs_do_not():
    cfg = _load_lsbf()
    patterns = [
        re.compile(pattern, re.IGNORECASE)
        for pattern in cfg.discovery.allow_url_patterns
    ]

    for url in (
        "https://lsbf.edu.my/programmes/bachelor-of-arts-honours-in-business-management/",
        "https://lsbf.edu.my/programmes/diploma-in-healthcare-management/",
        "https://www.lsbf.edu.my/programmes/certificate-in-information-technology/",
    ):
        assert _link_matches_post_discovery_allow({"url": url}, patterns)

    for url in (
        "https://lsbf.edu.my/programmes/",
        "https://lsbf.edu.my/certificates-program/",
        "https://lsbf.edu.my/diploma/",
        "https://lsbf.edu.my/bachelors-program/",
    ):
        assert not _link_matches_post_discovery_allow({"url": url}, patterns)


def test_lsbf_non_award_campaign_pages_are_blocked_without_hiding_courses():
    cfg = _load_lsbf()
    blocked = [
        re.compile(pattern, re.IGNORECASE)
        for pattern in cfg.discovery.block_url_patterns
    ]

    for url in (
        "https://lsbf.edu.my/programmes/young-learners-english-summer-camp-2026/",
        "https://lsbf.edu.my/programmes/pearson-pte-programme-flyer/",
        "https://lsbf.edu.my/programmes/ielts-preparatory-programmes/",
    ):
        assert any(pattern.search(url) for pattern in blocked)

    for url in (
        "https://lsbf.edu.my/programmes/foundation-in-science/",
        "https://lsbf.edu.my/programmes/certificate-in-english/",
        "https://lsbf.edu.my/programmes/acca-qualification/",
    ):
        assert not any(pattern.search(url) for pattern in blocked)


@pytest.mark.asyncio
async def test_lsbf_listing_discovers_foundation_acca_and_degree_pages(monkeypatch):
    listing_url = "https://lsbf.edu.my/programmes/"
    html = """
    <html><body><h1>Programmes</h1>
      <a href="/certificates-program/">Certificates</a>
      <a href="/diploma/">Diplomas</a>
      <a href="/bachelors-program/">Bachelors</a>
      <a href="/programmes/foundation-in-science/">Foundation in Science</a>
      <a href="/programmes/foundation-in-arts/">Foundation in Arts</a>
      <a href="/programmes/acca-foundation-in-accountancy/">ACCA Foundation in Accountancy</a>
      <a href="/programmes/acca-qualification/">ACCA Qualification</a>
      <a href="/programmes/acca-qualification-odl/">ACCA Qualification ODL</a>
      <a href="/programmes/bachelor-of-arts-honours-in-business-management/">
        Bachelor of Arts (Honours) in Business Management
      </a>
      <a href="/programmes/diploma-in-healthcare-management/">
        Diploma in Healthcare Management
      </a>
    </body></html>
    """

    async def fake_fetch_html(url, **kwargs):
        return html if url == listing_url else ""

    async def no_sitemap(*args, **kwargs):
        return []

    monkeypatch.setattr(discovery_mod, "fetch_html", fake_fetch_html)
    monkeypatch.setattr(sitemap_mod, "discover_from_sitemap", no_sitemap)

    cfg = _load_lsbf()
    results = await discovery_mod.discover_course_links(
        listing_url,
        max_pages=cfg.discovery.bfs_page_budget or 25,
        max_courses=100,
        discovery_config=cfg.discovery,
    )
    urls = {item["url"] for item in results}

    assert {
        "https://lsbf.edu.my/programmes/foundation-in-science/",
        "https://lsbf.edu.my/programmes/foundation-in-arts/",
        "https://lsbf.edu.my/programmes/acca-foundation-in-accountancy/",
        "https://lsbf.edu.my/programmes/acca-qualification/",
        "https://lsbf.edu.my/programmes/acca-qualification-odl/",
        "https://lsbf.edu.my/programmes/bachelor-of-arts-honours-in-business-management/",
        "https://lsbf.edu.my/programmes/diploma-in-healthcare-management/",
    }.issubset(urls)
    assert "https://lsbf.edu.my/certificates-program/" not in urls
    assert "https://lsbf.edu.my/diploma/" not in urls
    assert "https://lsbf.edu.my/bachelors-program/" not in urls