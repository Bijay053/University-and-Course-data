"""Regression coverage for AUT's Cloudflare discovery route."""

from pathlib import Path

import yaml


AUT_CONFIG = (
    Path(__file__).resolve().parents[1] / "scraper_config" / "unis" / "aut.yaml"
)
DISCOVERY_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "services"
    / "scraper"
    / "discovery.py"
)


def _raw_config() -> dict:
    return yaml.safe_load(AUT_CONFIG.read_text())


def test_aut_uses_render_first_for_cloudflare():
    raw = _raw_config()
    discovery = raw["discovery"]
    extraction = raw["extraction"]

    assert discovery["scrape_do_skip_fallbacks"] is True
    assert discovery["scrape_do_render"] is True
    assert discovery["skip_sitemap_fallback"] is True
    assert discovery["skip_browser_discovery"] is True
    assert discovery["use_wayback"] is False
    assert extraction["scrape_do_skip_fallbacks"] is True
    assert extraction["scrape_do_render"] is True
    assert extraction["skip_browser_rescue"] is True


def test_aut_rendered_seed_crawl_is_bounded():
    discovery = _raw_config()["discovery"]

    assert len(discovery["seed_urls"]) == 37
    assert discovery["seed_urls"][0] == "https://www.aut.ac.nz/study/study-options"
    assert discovery["bfs_page_budget"] == 40
    assert discovery["discovery_phase_timeout_s"] == 600
    assert "/study/study-options/?$" in discovery["listing_only_patterns"]
    assert "/study/study-options$" not in discovery["block_url_patterns"]


def test_aut_seed_queue_is_yaml_only_not_duplicated_in_python():
    source = DISCOVERY_SOURCE.read_text()

    assert "_aut_faculties" not in source
    assert "_aut_hosts" not in source