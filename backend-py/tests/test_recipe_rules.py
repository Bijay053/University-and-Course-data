from app.services.scraper.recipe_rules import _convert_full_course_to_annual
from app.services.scraper.recipe_rules import apply_recipe_rules


def test_subannual_full_course_fee_is_not_relabelled_or_converted():
    payload = {
        "international_fee": 48_300,
        "fee_term": "Full Course",
        "duration": 8,
        "duration_term": "Month",
    }

    _convert_full_course_to_annual(payload)

    assert payload["international_fee"] == 48_300
    assert payload["fee_term"] == "Full Course"


def test_full_course_fee_is_annualized_at_twelve_months_or_more():
    payload = {
        "international_fee": 96_000,
        "fee_term": "Full Course",
        "duration": 24,
        "duration_term": "Month",
    }

    _convert_full_course_to_annual(payload)

    assert payload["international_fee"] == 48_000
    assert payload["fee_term"] == "Annual"


def test_persisted_audience_recipe_cannot_write_domestic_row():
    recipe = {"audience_recipe": {
        "selectors": [{"container": "select:nth-of-type(1)", "option": "intl",
                       "audience": "international", "intake_months": [3]}],
        "english": {"central_page": "https://uni.example/english"},
    }}
    domestic = {"audience_identity": {
        "audience": "domestic", "container": "select:nth-of-type(1)", "option": "dom"
    }}
    apply_recipe_rules(domestic, recipe)
    assert "intake_months" not in domestic
    international = {"audience_identity": {
        "audience": "international", "container": "select:nth-of-type(1)", "option": "intl"
    }}
    apply_recipe_rules(international, recipe)
    assert international["intake_months"] == [3]
    assert international["english_source_url"].endswith("/english")