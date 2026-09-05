"""Regression coverage for CQU's sitemap-only discovery recipe."""

from app.services.scraper.config.loader import get_config_for_host
from app.services.scraper.discovery import _ALWAYS_SITEMAP_SUPPLEMENT_HOSTS


def test_cqu_verified_sitemap_recipe_overrides_stale_admin_bfs() -> None:
    cfg = get_config_for_host(
        hostname="www.cqu.edu.au",
        name="CQUniversity Australia",
        scrape_url="https://www.cqu.edu.au",
        university_id=22,
        db_scrape_config={
            "auto_config": {
                "discovery": {
                    "bfs_page_budget": 25,
                    "sitemap_url": None,
                    "always_sitemap_supplement": True,
                }
            },
            "admin_config": {
                "discovery": {
                    "bfs_page_budget": 60,
                    "sitemap_url": None,
                    "always_sitemap_supplement": True,
                }
            },
        },
        create_missing_stub=False,
    )

    assert cfg.discovery.bfs_page_budget == 0
    assert cfg.discovery.sitemap_url == "https://www.cqu.edu.au/sitemap.xml"
    assert cfg.discovery.always_sitemap_supplement is False
    assert "www.cqu.edu.au" not in _ALWAYS_SITEMAP_SUPPLEMENT_HOSTS
    assert "cqu.edu.au" not in _ALWAYS_SITEMAP_SUPPLEMENT_HOSTS