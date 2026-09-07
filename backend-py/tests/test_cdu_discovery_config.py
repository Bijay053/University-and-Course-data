"""Regression coverage for CDU's verified sitemap catalogue."""

from app.services.scraper.config.loader import get_config_for_host


def test_cdu_verified_recipe_overrides_generated_navigation_filter() -> None:
    cfg = get_config_for_host(
        hostname="www.cdu.edu.au",
        name="Charles Darwin University",
        scrape_url="https://www.cdu.edu.au",
        university_id=24,
        db_scrape_config={
            "admin_config": {"discovery": {"bfs_page_budget": 60, "allow_url_patterns": [r"cdu\.edu\.au/"]}},
            "auto_config": {"discovery": {"allow_url_patterns": [r"^/study/.+/undergraduate/.+"]}},
        },
        create_missing_stub=False,
    )

    assert cfg.discovery.bfs_page_budget == 0
    assert cfg.discovery.skip_browser_discovery is True
    assert cfg.discovery.sitemap_url == "https://www.cdu.edu.au/sitemap.xml"
    assert cfg.discovery.allow_url_patterns == [
        r"^https?://(?:www\.)?cdu\.edu\.au/study/course/[^/?]+(?:\?year=20\d{2})?$"
    ]