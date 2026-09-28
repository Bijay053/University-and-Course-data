"""ARU's verified catalogue recipe must survive stale generated repair rules."""

from app.services.scraper.config.loader import load_uni_config


def test_aru_verified_discovery_overrides_stale_generated_and_admin_filters():
    obsolete = {
        "discovery": {
            "seed_urls": ["https://aru-sc104-prod-uksouth-cd.azurewebsites.net/international/courses"],
            "sitemap_url": "https://aru.ac.uk/sitemap.xml",
            "allow_url_patterns": [
                r"^https://aru-sc104-prod-uksouth-cd\.azurewebsites\.net/international/courses/"
            ],
            "sitemap_loc_host_canonicalizations": [],
            "use_stealth_browser": False,
            "max_candidates": 2,
        }
    }
    config = load_uni_config(
        slug="aru",
        name="Anglia Ruskin University",
        scrape_url="https://www.aru.ac.uk",
        university_id=93,
        db_scrape_config={"auto_config": obsolete, "admin_config": obsolete},
        create_missing_stub=False,
        strict=True,
    )
    discovery = config.discovery
    assert discovery.sitemap_url == "https://www.aru.ac.uk/sitemap.xml"
    assert discovery.seed_urls == [
        "https://www.aru.ac.uk/study/course-search?levelofstudy=Undergraduate",
        "https://www.aru.ac.uk/study/course-search?levelofstudy=Postgraduate",
    ]
    assert discovery.allow_url_patterns == [
        "/study/undergraduate/",
        "/study/postgraduate/",
    ]
    assert discovery.max_candidates == 800
    assert discovery.use_stealth_browser is True
    assert len(discovery.sitemap_loc_host_canonicalizations) == 1
    rule = discovery.sitemap_loc_host_canonicalizations[0]
    assert rule.source_host == "aru-sc104-prod-uksouth-cd.azurewebsites.net"
    assert rule.origin == "https://www.aru.ac.uk"
    assert rule.allowed_path_prefixes == [
        "/study/undergraduate/",
        "/study/postgraduate/",
    ]