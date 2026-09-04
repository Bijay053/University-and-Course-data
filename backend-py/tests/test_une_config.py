from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services.scraper.config import set_uni_config
from app.services.scraper.config.loader import load_uni_config


@pytest.mark.parametrize("university_id", [15, 2288])
def test_une_duplicate_recipes_resolve_to_same_hardened_strategy(
    university_id: int,
) -> None:
    config = load_uni_config(
        slug="une",
        scrape_url="https://www.une.edu.au",
        university_id=university_id,
        name="University of New England",
    )

    assert config.discovery.sitemap_url == (
        "https://www.une.edu.au/cauc-static/study/sitemap.xml"
    )
    assert config.discovery.allow_url_patterns == [
        "/study/courses/[a-zA-Z0-9_-]+$"
    ]
    assert config.discovery.bfs_page_budget == 0
    assert config.discovery.always_sitemap_supplement is False
    assert config.discovery.use_wayback is False
    assert config.discovery.scrape_do_skip_fallbacks is True
    assert config.discovery.scrape_do_render is True
    assert config.discovery.skip_home_page_redirect is True

    assert config.extraction.scrape_do_render is True
    assert config.extraction.scrape_do_skip_fallbacks is True
    assert config.extraction.skip_browser_rescue is True
    assert config.extraction.skip_per_course_browser is True
    assert config.extraction.max_parallel_fetch == 3
    assert config.extraction.per_course_timeout_seconds == 90
    assert config.extraction.recovery_sweep_max_items == 3
    assert config.extraction.recovery_sweep_time_budget_seconds == 120
    assert config.extraction.fees.force_central_fee_stage is True
    assert (
        config.extraction.fees.require_explicit_international_context is True
    )
    assert config.extraction.english.default_ielts == 6.0
    assert config.extraction.english.default_pte == 57
    assert config.extraction.english.default_toefl == 79
    assert config.extraction.english.course_english_priority is True
    assert config.extraction.english.apply_defaults_before_remote_enrichment is True
    research = config.extraction.english.degree_level_defaults["research"]
    assert research.ielts == 6.5
    assert research.pte == 64
    assert research.toefl == 91


def test_une_slug_and_id_recipe_files_exist() -> None:
    from app.services.scraper.config import loader

    root = Path(__file__).resolve().parents[1] / "scraper_config" / "unis"
    slug_recipe = root / "une.yaml"
    id_recipe = root / "une_2288.yaml"
    assert slug_recipe.is_file()
    assert id_recipe.is_file()
    assert loader._declared_yaml_hostname(slug_recipe) == "www.une.edu.au"
    assert loader._declared_yaml_hostname(id_recipe) == "www.une.edu.au"


@pytest.mark.asyncio
async def test_une_discovery_fetch_goes_directly_to_render() -> None:
    from app.services.scraper import http_fetcher

    config = load_uni_config(
        slug="une",
        scrape_url="https://www.une.edu.au",
        university_id=15,
        name="University of New England",
    )
    set_uni_config(config)
    sitemap_html = "<html>" + ("course sitemap " * 100) + "</html>"

    with (
        patch.object(
            http_fetcher,
            "fetch_html_scrape_do",
            new=AsyncMock(return_value=sitemap_html),
        ) as scrape_do,
        patch.object(
            http_fetcher,
            "_get_shared_client",
            side_effect=AssertionError("direct HTTP must not run"),
        ) as direct,
        patch.object(
            http_fetcher,
            "fetch_html_cffi",
            new=AsyncMock(side_effect=AssertionError("curl_cffi must not run")),
        ) as cffi,
    ):
        html = await http_fetcher.fetch_html(config.discovery.sitemap_url)

    assert html == sitemap_html
    scrape_do.assert_awaited_once()
    assert scrape_do.call_args.kwargs["render"] is True
    direct.assert_not_called()
    cffi.assert_not_awaited()