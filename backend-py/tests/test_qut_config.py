from app.services.scraper.config.loader import load_uni_config


def _load_qut_config():
    return load_uni_config(
        slug="qut",
        scrape_url="https://www.qut.edu.au/",
        university_id=1011,
        name="Queensland University of Technology",
    )


def test_qut_uses_static_course_payload_without_browser_rescue() -> None:
    config = _load_qut_config()

    assert config.extraction.skip_per_course_browser is True


def test_qut_requires_strong_online_only_evidence() -> None:
    config = _load_qut_config()

    assert config.extraction.study_mode.online_only_requires_strong_evidence is True


def test_qut_canonical_course_titles_do_not_require_degree_qualifier() -> None:
    config = _load_qut_config()

    assert config.extraction.staging.skip_degree_qualifier_check is True