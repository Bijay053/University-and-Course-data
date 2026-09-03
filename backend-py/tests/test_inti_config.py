from app.services.scraper.config.loader import load_uni_config


def test_inti_hostname_recipe_loads_for_production_database_id() -> None:
    config = load_uni_config(
        slug="newinti",
        scrape_url="https://newinti.edu.my/",
        university_id=11,
        name="INTI International University & Colleges",
    )

    assert config.discovery.allow_url_patterns == ["/programme/"]
    assert config.extraction.study_mode.suppress_nav_rule is True
    assert config.extraction.fees.default_currency == "MYR"