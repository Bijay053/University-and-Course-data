import pytest

from app.services.scraper import discovery as discovery_mod
from app.services.scraper import sitemap as sitemap_mod
from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.recipe_rules import apply_recipe_rules


def test_inti_hostname_recipe_loads_for_production_database_id() -> None:
    config = load_uni_config(
        slug="newinti",
        scrape_url="https://newinti.edu.my/",
        university_id=11,
        name="INTI International University & Colleges",
    )

    assert config.discovery.allow_url_patterns == ["/programme/"]
    assert config.discovery.seed_urls == [
        "https://newinti.edu.my/find-a-programme/"
    ]
    assert config.discovery.bfs_page_budget == 1
    assert config.discovery.force_candidate_url_patterns
    assert config.discovery.course_detail_url_patterns
    assert config.extraction.study_mode.suppress_nav_rule is True
    assert config.extraction.fees.default_currency == "MYR"
    assert config.extraction.fees.currency_override == "MYR"
    assert config.extraction.fees.fee_term == "Full Course"
    assert config.extraction.fees.fee_calculation_mode == "use_source_value_only"
    assert config.extraction.fees.fee_prevent_full_course_rollup is False


def test_inti_fee_recipe_preserves_full_programme_total_in_myr() -> None:
    config = load_uni_config(
        slug="newinti",
        scrape_url="https://newinti.edu.my/",
        university_id=11,
        name="INTI International University & Colleges",
    )
    payload = {
        "international_fee": 70_002,
        "currency": "AUD",
        "fee_currency": "AUD",
        "fee_term": "Annual",
        "duration": 2,
        "duration_term": "Year",
    }

    recipe = config.extraction.fees.model_dump()
    result = apply_recipe_rules(payload, recipe)

    assert result["international_fee"] == 70_002
    assert result["currency"] == "MYR"
    assert result["fee_currency"] == "MYR"
    assert result["fee_term"] == "Full Course"


@pytest.mark.asyncio
async def test_inti_finder_collects_all_programmes_from_one_page(
    monkeypatch,
) -> None:
    finder_url = "https://newinti.edu.my/find-a-programme/"
    programme_links = "\n".join(
        f'<a href="/programme/programme-{number:03d}/">'
        f"Programme {number:03d}</a>"
        for number in range(1, 150)
    )
    html = (
        "<html><body>"
        + programme_links
        + '<a href="/programme/professional-development-programme/">'
        + "Professional Development Programme</a>"
        + '<a href="/programme/micro-credential/">Micro credentials</a>'
        + '<a href="/academic-programmes/business/">Business category</a>'
        + "</body></html>"
    )

    async def _fake_fetch_html(url, retries=0):
        return html if url == finder_url else None

    async def _no_sitemap(*args, **kwargs):
        return []

    monkeypatch.setattr(discovery_mod, "fetch_html", _fake_fetch_html)
    monkeypatch.setattr(sitemap_mod, "discover_from_sitemap", _no_sitemap)

    config = load_uni_config(
        slug="newinti",
        scrape_url="https://newinti.edu.my/",
        university_id=11,
        name="INTI International University & Colleges",
    )
    results = await discovery_mod.discover_course_links(
        finder_url,
        max_pages=config.discovery.bfs_page_budget,
        max_courses=1000,
        discovery_config=config.discovery,
    )

    urls = {item["url"] for item in results}
    assert len(urls) == 150
    assert (
        "https://newinti.edu.my/programme/programme-001/" in urls
    )
    assert (
        "https://newinti.edu.my/programme/programme-149/" in urls
    )
    assert (
        "https://newinti.edu.my/programme/professional-development-programme/"
        in urls
    )
    assert not any("micro-credential" in url for url in urls)
    assert not any("/academic-programmes/" in url for url in urls)