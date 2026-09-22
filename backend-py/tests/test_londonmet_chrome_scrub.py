import pytest

from app.services.scraper import central_pages
from app.services.scraper.extractors.londonmet_chrome_scrub import (
    extract_real_fees,
    has_international_options,
    is_londonmet_host,
    parse_data_cost_entries,
)
from app.services.scraper.central_pages import (
    _parse_londonmet_undergraduate_english,
    _parse_londonmet_undergraduate_english_programs,
)
from app.services.scraper.config.loader import get_config_for_host
from app.services.scraper.pipelines.single_course import (
    _central_english_audience_mismatch,
    _normalise_central_english_fields,
    _select_central_english_program,
)


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


def test_londonmet_named_ug_scores_override_only_exact_linked_courses():
    html = """
    <button aria-controls="higher-panel">Higher requirements</button>
    <div id="higher-panel">
      <ul>
        <li>
          <a href="/courses/undergraduate/human-nutrition---bsc-hons/">
            Human Nutrition BSc
          </a>
          (English language which must not be less than 6.5, with no individual
          section less than 6.0)
        </li>
        <li>
          <a href="/courses/undergraduate/physiotherapy---bsc-hons/">
            Physiotherapy BSc
          </a>
          (Overall 7 with no less than 6.5 in each component)
        </li>
        <li>
          <a href="/courses/undergraduate/social-work---bsc-hons/">
            Social Work BSc
          </a>
          and
          <a href="/courses/undergraduate/social-work-including-foundation-year---bsc-hons/">
            Social Work (including foundation year) BSc
          </a>
          (Overall 7 with no less than 6.5 in each component)
        </li>
        <li>
          <a href="/courses/undergraduate/dietetics---bsc-hons/">
            Dietetics BSc
          </a>
          and
          <a href="/courses/undergraduate/dietetics-and-nutrition---bsc-hons/">
            Dietetics and Nutrition BSc
          </a>
          (Overall 7 with no less than 6.5 in each component)
        </li>
        <li>
          <a href="/courses/undergraduate/biomedical-science---bsc-hons/">
            Biomedical Science BSc
          </a>
          (Overall 6 is accepted for IELTS)
        </li>
      </ul>
      <h3>Academic IELTS</h3>
      <p>Overall score of 6.5 with 6.0 in each component</p>
    </div>
    """
    source_url = (
        "https://www.londonmet.ac.uk/international/applying/"
        "english-language-requirements/undergraduate/"
    )
    profiles = _parse_londonmet_undergraduate_english_programs(html, source_url)

    assert len(profiles) == 6
    selected = _select_central_english_program(
        profiles,
        "Social Work - BSc (Hons)",
        "https://www.londonmet.ac.uk/courses/undergraduate/"
        "social-work---bsc-hons/",
    )
    assert _normalise_central_english_fields(selected) == {
        "ielts_overall": 7.0,
        "ielts_listening": 6.5,
        "ielts_reading": 6.5,
        "ielts_writing": 6.5,
        "ielts_speaking": 6.5,
    }
    assert _normalise_central_english_fields(
        _select_central_english_program(
            profiles,
            "Human Nutrition - BSc (Hons)",
            "https://www.londonmet.ac.uk/courses/undergraduate/"
            "human-nutrition---bsc-hons/",
        )
    ) == {
        "ielts_overall": 6.5,
        "ielts_listening": 6.0,
        "ielts_reading": 6.0,
        "ielts_writing": 6.0,
        "ielts_speaking": 6.0,
    }
    assert _select_central_english_program(
        profiles,
        "Biomedical Science - BSc (Hons)",
        "https://www.londonmet.ac.uk/courses/undergraduate/"
        "biomedical-science---bsc-hons/",
    ) == {}
    assert _select_central_english_program(
        profiles,
        "International Business - BA (Hons)",
        "https://www.londonmet.ac.uk/courses/undergraduate/"
        "international-business---ba-hons/",
    ) == {}


def test_londonmet_named_profiles_require_official_ug_scope():
    html = """
    <button aria-controls="higher-panel">Higher requirements</button>
    <div id="higher-panel"><ul><li>
      <a href="/courses/undergraduate/social-work---bsc-hons/">Social Work BSc</a>
      (Overall 7 with no less than 6.5 in each component)
    </li></ul></div>
    """
    assert _parse_londonmet_undergraduate_english_programs(
        html, "https://example.edu/undergraduate/"
    ) == []


@pytest.mark.asyncio
async def test_londonmet_split_ug_prefetch_returns_default_and_named_profiles(
    monkeypatch,
):
    html = """
    <h3>IELTS</h3>
    <p>Overall score of 6.0 with 5.5 in each component</p>
    <button aria-controls="higher-panel">Higher requirements</button>
    <div id="higher-panel">
      <ul><li>
        <a href="/courses/undergraduate/social-work---bsc-hons/">
          Social Work BSc
        </a>
        (Overall 7 with no less than 6.5 in each component)
      </li></ul>
      <h3>Academic IELTS</h3>
      <p>Overall score of 6.5 with 6.0 in each component</p>
    </div>
    """

    async def fake_fetch_html(_url):
        return html

    monkeypatch.setattr(central_pages, "fetch_html", fake_fetch_html)
    result = await central_pages.prefetch_central_pages(
        {
            "uniPages": {
                "entryPageUG": (
                    "https://www.londonmet.ac.uk/international/applying/"
                    "english-language-requirements/undergraduate/"
                )
            }
        }
    )

    assert result["english"] == {}
    assert result["english_by_level"]["undergraduate"] == {
        "ielts_overall": 6.0,
        "ielts_minimum": 5.5,
    }
    assert result["english_by_program"] == [
        {
            "program_names": "Social Work BSc",
            "program_aliases": ["Social Work BSc"],
            "course_codes": ["social-work---bsc-hons"],
            "values": {"ielts_overall": 7.0, "ielts_minimum": 6.5},
        }
    ]


def test_generic_central_english_survives_without_audience_identity_and_expands_bands():
    central = {
        "english_by_level": {
            "undergraduate": {
                "ielts_overall": 6.0,
                "ielts_minimum": 5.5,
            }
        }
    }

    assert not _central_english_audience_mismatch(
        central,
        None,
        identity_matches=False,
    )
    assert _normalise_central_english_fields(
        central["english_by_level"]["undergraduate"]
    ) == {
        "ielts_overall": 6.0,
        "ielts_listening": 5.5,
        "ielts_reading": 5.5,
        "ielts_writing": 5.5,
        "ielts_speaking": 5.5,
    }


def test_audience_scoped_central_english_still_fails_closed_on_mismatch():
    central = {
        "english": {"ielts_overall": 6.0},
        "audience_recipe": {"selectors": [{"audience": "international"}]},
    }

    assert _central_english_audience_mismatch(
        central,
        {"audience": "domestic"},
        identity_matches=False,
    )


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


def test_duration_uses_current_overseas_fulltime_option_without_requiring_fee():
    html = """
    <select id="course-entry-point-selector">
      <optgroup label="UK">
        <option data-mode="Full-time" data-duration="3 years"
                data-m="September" data-y="2026">September 2026</option>
      </optgroup>
      <optgroup label="Overseas">
        <option data-mode="Full-time" data-duration="2 years"
                data-m="September" data-y="2025">September 2025</option>
        <option data-mode="Part-time" data-duration="6 years"
                data-m="September" data-y="2027">September 2027</option>
        <option data-mode="Full-time" data-duration="4 years"
                data-m="September" data-y="2027">September 2027</option>
        <option data-mode="Full-time" data-duration="5 years"
                data-m="September" data-y="2028">September 2028</option>
      </optgroup>
    </select>
    """
    result = extract_real_fees(
        parse_data_cost_entries(html), current_year=2026
    )
    assert result["duration"] == "4 years"
    assert result["intake_months"] == [9]


def test_conflicting_current_overseas_fulltime_durations_fail_closed():
    html = """
    <select id="course-entry-point-selector">
      <optgroup label="Overseas">
        <option data-mode="Full-time" data-duration="3 years"
                data-m="January" data-y="2027">January 2027</option>
        <option data-mode="Full-time" data-duration="4 years"
                data-m="September" data-y="2027">September 2027</option>
      </optgroup>
    </select>
    """
    result = extract_real_fees(
        parse_data_cost_entries(html), current_year=2026
    )
    assert "duration" not in result


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
    # London Met publishes course-specific exceptions.  Unverified blanket
    # defaults (especially alternative-test scores) must not fill blank slots.
    assert config.extraction.english.default_ielts is None
    assert config.extraction.english.default_pte is None
    assert config.extraction.english.default_toefl is None