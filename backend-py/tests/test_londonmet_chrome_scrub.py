from app.services.scraper.extractors.londonmet_chrome_scrub import (
    extract_real_fees,
    has_international_options,
    is_londonmet_host,
    parse_data_cost_entries,
)
from app.services.scraper.central_pages import _parse_londonmet_undergraduate_english
from app.services.scraper.config.loader import get_config_for_host


def test_overseas_entry_points_keep_all_overseas_months_not_uk_months():
    html = """
    <select id="course-entry-point-selector">
      <option data-fee-type="UK" data-mode="Full-time"
              data-m="September" data-y="2027">September 2027 - Full-time</option>
      <option data-fee-type="UK" data-mode="Full-time"
              data-m="January" data-y="2027">January 2027 - Full-time</option>
      <option data-fee-type="International" data-mode="Full-time"
              data-m="January" data-y="2027">January 2027 - Full-time</option>
      <option data-fee-type="International" data-mode="Full-time"
              data-m="September" data-y="2027">September 2027 - Full-time</option>
    </select>
    """

    entries = parse_data_cost_entries(html)
    assert extract_real_fees(entries) == {"intake_months": [1, 9]}


def test_overseas_intakes_do_not_use_page_wide_dates():
    html = """
    <p>Applications close in October.</p>
    <select id="course-entry-point-selector">
      <option data-fee-type="UK" data-mode="Full-time"
              data-m="September" data-y="2027">September 2027 - Full-time</option>
      <option data-fee-type="International" data-mode="Full-time"
              data-m="January" data-y="2027">January 2027 - Full-time</option>
    </select>
    """

    assert extract_real_fees(parse_data_cost_entries(html)) == {"intake_months": [1]}


def test_londonmet_english_uses_standard_ug_row_not_exception_section():
    html = """
    <h3>IELTS</h3>
    <p>Overall score of 6.0 with 5.5 in each component</p>
    <h3>Exceptions</h3>
    <p>Social Work BSc: Overall score of 7.0 with 6.5 in each component</p>
    <h3>Academic IELTS</h3>
    <p>Overall score of 6.5 with 6.0 in each component</p>
    """
    assert _parse_londonmet_undergraduate_english(
        html,
        "https://www.londonmet.ac.uk/international/applying/english-language-requirements/undergraduate/",
    ) == {"ielts_overall": 6.0, "ielts_minimum": 5.5}


def test_entry_points_are_selector_scoped_and_exclude_past_cohorts():
    html = """
    <div data-fee-type="International" data-m="March" data-y="2027"></div>
    <select id="course-entry-point-selector">
      <optgroup label="UK">
        <option data-fee-type="UK" data-mode="Full-time"
                data-m="March" data-y="2027">March 2027</option>
      </optgroup>
      <optgroup label="Overseas">
        <option data-mode="Full-time" data-m="October" data-y="2025">
          October 2025
        </option>
        <option data-mode="Full-time" data-m="January" data-y="2027">
          January 2027
        </option>
        <option data-fee-type="International" data-mode="Full-time"
                data-m="September" data-y="2027">September 2027</option>
      </optgroup>
    </select>
    """
    entries = parse_data_cost_entries(html)
    assert extract_real_fees(entries, current_year=2026) == {
        "intake_months": [1, 9]
    }


def test_unrelated_international_markup_does_not_make_selector_international():
    html = """
    <div data-fee-type="International">navigation metadata</div>
    <select id="course-entry-point-selector">
      <option data-fee-type="UK" data-m="September" data-y="2027">
        September 2027
      </option>
    </select>
    """
    assert parse_data_cost_entries(html) == [
        {
            "cost": None,
            "fee_type": "UK",
            "mode": "",
            "month": "september",
            "year": 2027,
            "location": None,
            "duration": None,
            "per_year": False,
        }
    ]
    assert has_international_options(html) is False


def test_mixed_current_cohort_fees_remain_unresolved():
    html = """
    <select id="course-entry-point-selector">
      <option data-fee-type="International" data-mode="Full-time"
              data-cost="£17,000 per year" data-m="January" data-y="2027">
        January 2027
      </option>
      <option data-fee-type="International" data-mode="Full-time"
              data-cost="£18,000 per year" data-m="September" data-y="2027">
        September 2027
      </option>
    </select>
    """
    result = extract_real_fees(
        parse_data_cost_entries(html), current_year=2026
    )
    assert result["intake_months"] == [1, 9]
    assert "international_fee" not in result


def test_selected_cohort_does_not_mix_future_year_months():
    html = """
    <select id="course-entry-point-selector">
      <option data-fee-type="International" data-mode="Full-time"
              data-m="January" data-y="2027">January 2027</option>
      <option data-fee-type="International" data-mode="Full-time"
              data-m="September" data-y="2027">September 2027</option>
      <option data-fee-type="International" data-mode="Full-time"
              data-m="March" data-y="2028">March 2028</option>
    </select>
    """
    assert extract_real_fees(
        parse_data_cost_entries(html), current_year=2026
    ) == {"intake_months": [1, 9]}


def test_past_only_dated_international_cohort_is_unresolved():
    html = """
    <select id="course-entry-point-selector">
      <option data-fee-type="International" data-mode="Full-time"
              data-cost="£17,000 per year" data-m="September" data-y="2025">
        September 2025
      </option>
    </select>
    """
    assert extract_real_fees(
        parse_data_cost_entries(html), current_year=2026
    ) == {}


def test_yearless_international_entries_are_used_only_without_dated_cohorts():
    yearless = """
    <select id="course-entry-point-selector">
      <option data-fee-type="International" data-mode="Full-time"
              data-m="September">September</option>
    </select>
    """
    assert extract_real_fees(
        parse_data_cost_entries(yearless), current_year=2026
    ) == {"intake_months": [9]}

    mixed = yearless.replace(
        "</select>",
        '<option data-fee-type="International" data-mode="Full-time" '
        'data-m="January" data-y="2027">January 2027</option></select>',
    )
    assert extract_real_fees(
        parse_data_cost_entries(mixed), current_year=2026
    ) == {"intake_months": [1]}


def test_londonmet_recipe_retains_modeled_ug_central_source():
    assert is_londonmet_host("https://www.londonmet.ac.uk:443/courses/x")
    config = get_config_for_host(
        hostname="www.londonmet.ac.uk",
        name="London Metropolitan University",
        scrape_url="https://www.londonmet.ac.uk",
        university_id=2227,
        db_scrape_config={},
    )
    assert config.extraction.english.central_page_ug.endswith(
        "/international/applying/english-language-requirements/undergraduate/"
    )