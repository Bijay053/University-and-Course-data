from __future__ import annotations

import pytest

from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.orchestrator import _apply_render_listing_pages


def _uel_config():
    return load_uni_config(
        slug="uel",
        scrape_url="https://www.uel.ac.uk",
        university_id=69,
        name="University of East London",
    )


def test_uel_uses_static_proxy_catalogues_instead_of_dead_discovery_tiers():
    discovery = _uel_config().discovery

    assert discovery.always_browser_discover is True
    assert discovery.skip_browser_discovery is True
    assert discovery.use_wayback is False
    assert discovery.render_listing_pages_static is True
    assert discovery.render_listing_pages == [
        "https://www.uel.ac.uk/study/undergraduate/courses",
        "https://www.uel.ac.uk/study/postgraduate/courses",
    ]
    assert discovery.allow_url_patterns == [
        r"^https://www\.uel\.ac\.uk/(undergraduate|postgraduate)/courses/[^/?#]+/?$"
    ]


def test_uel_browser_extraction_overrides_stale_auto_config_suppression():
    config = load_uni_config(
        slug="uel",
        scrape_url="https://www.uel.ac.uk",
        university_id=69,
        name="University of East London",
        db_scrape_config={
            "auto_config": {
                "extraction": {
                    "skip_browser_rescue": True,
                    "skip_per_course_browser": True,
                },
            },
        },
    )

    assert config.extraction.force_browser is True
    assert config.extraction.skip_initial_http_fetch is True
    assert config.extraction.skip_browser_rescue is False
    assert config.extraction.skip_per_course_browser is False


@pytest.mark.asyncio
async def test_uel_static_catalogues_harvest_only_course_detail_urls():
    discovery = _uel_config().discovery
    fetch_modes: list[bool] = []

    async def fake_fetch(url: str, *, render: bool, **_kwargs) -> str:
        fetch_modes.append(render)
        if "undergraduate" in url:
            return """
                <a href="/undergraduate/courses/bsc-hons-data-science">
                  Data Science and Artificial Intelligence BSc (Hons)
                </a>
                <a href="/study/undergraduate/fees">Fees</a>
            """
        return """
            <a href="/postgraduate/courses/msc-project-management">
              Project Management MSc
            </a>
            <a href="/study/postgraduate/courses">Course listing</a>
        """

    links: list[dict] = []
    added = await _apply_render_listing_pages(
        links=links,
        scrape_url="https://www.uel.ac.uk",
        render_pages=list(discovery.render_listing_pages),
        allow_patterns=list(discovery.allow_url_patterns),
        block_patterns=list(discovery.block_url_patterns),
        render_static=discovery.render_listing_pages_static,
        _fetch_fn=fake_fetch,
    )

    assert added == 2
    assert fetch_modes == [False, False]
    assert links == [
        {
            "url": (
                "https://www.uel.ac.uk/undergraduate/courses/"
                "bsc-hons-data-science"
            ),
            "name": "",
        },
        {
            "url": (
                "https://www.uel.ac.uk/postgraduate/courses/"
                "msc-project-management"
            ),
            "name": "",
        },
    ]