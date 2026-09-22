"""WLV SearchStax course documents must not be re-filtered by stale URL rules."""
from pathlib import Path

from app.services.scraper.config.loader import load_uni_config


def test_wlv_links_only_provider_owns_its_course_url_set():
    cfg = load_uni_config(
        slug="wlv",
        name="University of Wolverhampton",
        scrape_url="https://www.wlv.ac.uk",
        university_id=1761,
        create_missing_stub=False,
    )
    assert cfg.discovery.searchstax is not None
    assert cfg.discovery.searchstax.links_only is True
    assert cfg.discovery.searchstax.url_base == "https://www.wlv.ac.uk/courses"


def test_production_wlv_id_uses_static_proxy_for_course_pages():
    cfg = load_uni_config(
        slug="wlv",
        name="University of Wolverhampton",
        scrape_url="https://www.wlv.ac.uk",
        university_id=74,
        create_missing_stub=False,
    )
    assert cfg.extraction.scrape_do_static is True
    assert cfg.extraction.staging.skip_duplicate_fee_check is True


def test_provider_bypass_covers_final_course_detail_gate():
    source = Path("app/services/scraper/orchestrator.py").read_text()
    assert "_skip_url_filters_searchstax = _provider_owns_current_links" in source
    assert (
        "if _cdp_raw and links and not _skip_url_filters_searchstax:"
        in source
    ), (
        "Provider-owned course URLs bypass allow/block filters and must also "
        "bypass stale course_detail_url_patterns from admin or auto config."
    )


def test_provider_bypass_is_not_based_on_configuration_presence():
    source = Path("app/services/scraper/orchestrator.py").read_text()
    bypass_assignment = next(
        line.strip() for line in source.splitlines()
        if line.strip().startswith("_skip_url_filters_searchstax =")
    )
    assert bypass_assignment == (
        "_skip_url_filters_searchstax = _provider_owns_current_links"
    )