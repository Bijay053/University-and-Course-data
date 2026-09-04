from pathlib import Path
import re
from unittest.mock import AsyncMock, patch

import pytest

from app.services.scraper.config import set_uni_config
from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.central_pages import _parse_fee_page_html, match_central_fee


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
        r"/study/courses/[a-zA-Z0-9_-]+(?:\?international=true)?$"
    ]
    allow = re.compile(config.discovery.allow_url_patterns[0])
    assert allow.search(
        "https://www.une.edu.au/study/courses/master-of-education-research"
    )
    assert allow.search(
        "https://www.une.edu.au/study/courses/"
        "master-of-education-research?international=true"
    )
    assert not allow.search(
        "https://www.une.edu.au/study/courses/"
        "master-of-education-research?domestic=true"
    )
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
    assert config.extraction.fees.central_fee_exact_match_only is True
    assert config.extraction.fees.central_page == (
        "https://www.une.edu.au/international/fees-and-scholarships/"
        "course-fees-2027"
    )
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


def test_une_central_fee_table_accepts_declared_aud_annual_bare_amounts() -> None:
    html = """
    <main>
      <h1>Course fees - 2027</h1>
      <p>All fees are quoted in Australian (AUD) dollars.</p>
      <p>*Annual course fees only cover the cost of tuition.</p>
      <table>
        <tr>
          <th>Courses available to International Students in 2027</th>
          <th>Intakes</th><th>CRICOS</th><th>Fee</th>
        </tr>
        <tr>
          <td>Courses available to International Students in 2027 Bachelor of Biomedical Science</td>
          <td>Intakes 1 &amp; 2 &amp; 3</td><td>CRICOS 061315J</td><td>Fee 35,808*</td>
        </tr>
        <tr>
          <td>Courses available to International Students in 2027 Master of Professional Accounting</td>
          <td>Intakes 1 &amp; 2</td><td>CRICOS 084168C</td><td>Fee 37,296*</td>
        </tr>
      </table>
    </main>
    """

    records = _parse_fee_page_html(
        html,
        "https://www.une.edu.au/international/fees-and-scholarships/"
        "course-fees-2027",
    )

    assert [(r["program_pattern"], r["international_fee"], r["per"]) for r in records] == [
        ("Bachelor of Biomedical Science", 35_808, "Annual"),
        ("Master of Professional Accounting", 37_296, "Annual"),
    ]


def test_une_exact_only_fee_matching_rejects_nearby_award_name() -> None:
    central_fees = [
        {
            "program_pattern": "Graduate Certificate in Business",
            "international_fee": 17_736,
            "currency": "AUD",
            "per": "Annual",
        },
        {
            "program_pattern": "Graduate Certificate in Data Science",
            "international_fee": 17_904,
            "currency": "AUD",
            "per": "Annual",
        },
    ]

    exact, confidence = match_central_fee(
        "Graduate Certificate in Data Science",
        central_fees,
        exact_only=True,
    )
    assert confidence == "exact"
    assert exact["international_fee"] == 17_904

    absent, confidence = match_central_fee(
        "Graduate Certificate in Agribusiness",
        central_fees,
        exact_only=True,
    )
    assert absent is None
    assert confidence == "none"