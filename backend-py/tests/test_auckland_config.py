from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.guards import filter_non_degree_candidates


def _config():
    return load_uni_config(
        slug="auckland",
        scrape_url="https://www.auckland.ac.nz/",
        name="University of Auckland",
    )


def test_auckland_subject_landing_pages_are_dropped_before_extraction():
    cfg = _config().discovery.non_degree_classifier
    links = [
        {
            "url": "https://www.auckland.ac.nz/en/study/study-options/"
            "find-a-study-option/japanese.html",
            "name": "Subject name: Japanese Faculty: Arts and Education "
            "Type: Undergraduate subject",
        },
        {
            "url": "https://www.auckland.ac.nz/en/study/study-options/"
            "find-a-study-option/bachelor-of-arts-ba.html",
            "name": "Programme name: Bachelor of Arts Faculty: Arts and Education "
            "Type: Bachelors degree",
        },
    ]
    kept, dropped = filter_non_degree_candidates(
        links,
        enabled=cfg.enabled,
        allow_url_patterns=cfg.allow_url_patterns,
        allow_title_patterns=cfg.allow_title_patterns,
        force_url_patterns=cfg.force_url_patterns,
        force_title_patterns=cfg.force_title_patterns,
    )
    assert kept == [links[1]]
    assert dropped == [links[0] | {"non_degree_reason": "forced_title_pattern"}]


def test_auckland_subject_filter_is_anchored_to_discovery_metadata():
    cfg = _config().discovery.non_degree_classifier
    kept, dropped = filter_non_degree_candidates(
        [{
            "url": "https://www.auckland.ac.nz/en/study/study-options/"
            "find-a-study-option/master-of-teaching.html",
            "name": "Programme name: Master of Teaching Subject name: Education",
        }],
        force_title_patterns=cfg.force_title_patterns,
    )
    assert len(kept) == 1
    assert dropped == []