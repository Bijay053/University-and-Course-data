"""Regression coverage for Bath Spa discovery-tier selection."""

from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.orchestrator import _browser_discovery_policy


def test_bathspa_browser_suppression_does_not_disable_bfs() -> None:
    config = load_uni_config(
        slug="bathspa",
        scrape_url="https://www.bathspa.ac.uk",
        university_id=76,
        name="Bath Spa University",
        db_scrape_config={
            "admin_config": {
                "discovery": {
                    "always_browser_discover": True,
                },
            },
        },
        create_missing_stub=False,
    )

    assert config.discovery.always_browser_discover is True
    assert config.discovery.skip_browser_discovery is True
    assert _browser_discovery_policy(config.discovery) == (False, True)
