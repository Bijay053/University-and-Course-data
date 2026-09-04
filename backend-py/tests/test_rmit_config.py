import re
from urllib.parse import urlparse

from app.services.scraper.config.loader import load_uni_config


def _load_rmit(db_scrape_config=None):
    return load_uni_config(
        slug="rmit",
        name="RMIT University",
        scrape_url="https://www.rmit.edu.au",
        university_id=17,
        db_scrape_config=db_scrape_config,
    )


def test_rmit_verified_discovery_cannot_be_replaced_by_stale_ai_admin_rules():
    config = _load_rmit({
        "auto_config": {
            "discovery": {
                "skip_sitemap_fallback": True,
                "static_course_urls_file": "wrong-library-pages.txt",
            },
            "extraction": {
                "fees": {
                    "require_explicit_international_context": False,
                }
            },
        },
        "admin_config": {
            "discovery": {
                "sitemap_url": "https://www.rmit.edu.au/wrong.xml",
                "allow_url_patterns": [
                    r"/study\-with\-us/levels\-of\-study/online/[^/]+/?$"
                ],
                "skip_sitemap_fallback": True,
                "bfs_page_budget": 99,
            },
            "extraction": {
                "fees": {
                    "require_explicit_international_context": False,
                }
            },
        },
    })

    assert config.discovery.sitemap_url == (
        "https://www.rmit.edu.au/study-with-us/sitemap.xml"
    )
    assert config.discovery.skip_sitemap_fallback is False
    assert config.discovery.use_wayback is False
    assert config.discovery.skip_browser_discovery is True
    assert config.discovery.bfs_page_budget == 0
    assert config.discovery.static_course_urls_file is None
    assert config.extraction.fees.require_explicit_international_context is True


def test_rmit_detail_pattern_accepts_main_awards_and_rejects_online_children():
    config = _load_rmit()
    patterns = [re.compile(pattern) for pattern in config.discovery.allow_url_patterns]
    urls = {
        "undergraduate": (
            "https://www.rmit.edu.au/study-with-us/levels-of-study/"
            "undergraduate-study/honours-degrees/"
            "bachelor-of-computer-science-honours-bh013"
        ),
        "postgraduate": (
            "https://www.rmit.edu.au/study-with-us/levels-of-study/"
            "postgraduate-study/masters-by-coursework/master-of-data-science-mc292"
        ),
        "online": (
            "https://www.rmit.edu.au/study-with-us/levels-of-study/online/"
            "online-graduate-certificate-in-leadership-gc215o"
        ),
        "child": (
            "https://www.rmit.edu.au/study-with-us/levels-of-study/"
            "undergraduate-study/honours-degrees/"
            "bachelor-of-computer-science-honours-bh013/apply-now"
        ),
    }

    def matches(url: str) -> bool:
        path = urlparse(url).path
        return any(pattern.search(url) and pattern.search(path) for pattern in patterns)

    assert matches(urls["undergraduate"])
    assert matches(urls["postgraduate"])
    assert not matches(urls["online"])
    assert not matches(urls["child"])