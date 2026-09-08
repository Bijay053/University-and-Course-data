from app.services.scraper.recipe_rules import _convert_full_course_to_annual


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