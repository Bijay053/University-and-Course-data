from pathlib import Path
import re

import yaml


def test_hope_hubs_are_blocked_without_losing_current_awards():
    config = yaml.safe_load((
        Path(__file__).parents[1] / "scraper_config/unis/hope_2221.yaml"
    ).read_text())
    patterns = config["discovery"]["block_url_patterns"]
    for path in (
        "/undergraduate/degreewithfoundationyear/",
        "/undergraduate/parentsandsupporters/",
        "/undergraduate/outreach/",
        "/undergraduate/networkofhope/",
        "/undergraduate/getaprospectus/",
        "/undergraduate/getaprospectus/orderaprospectus/",
        "/postgraduateresearch/",
    ):
        assert any(re.search(p, "https://www.hope.ac.uk" + path) for p in patterns)
    for path in (
        "/courses/digital-marketing/",
        "/courses/primary-education-5-11-with-qts/",
        "/courses/early-education-primary-3-7-with-qts/",
        "/postgraduate/postgraduatecourses/pgceprimary5-11/",
    ):
        assert not any(re.search(p, "https://www.hope.ac.uk" + path) for p in patterns)
    assert config["extraction"]["staging"]["skip_degree_qualifier_check"] is False
    assert config["extraction"]["english"]["central_page"] == "https://www.hope.ac.uk/englishlanguagerequirements/"
    assert config["extraction"]["fees"]["require_explicit_international_context"] is True


def test_international_partners_are_not_fee_audience_evidence():
    from app.services.scraper.extractors.fee import _INTL_CTX
    assert not _INTL_CTX.search("Teaching with our international partners in France.")
    assert not _INTL_CTX.search("International Relations BA")
    assert _INTL_CTX.search("International tuition fees")
    assert _INTL_CTX.search("Fees for international students")
