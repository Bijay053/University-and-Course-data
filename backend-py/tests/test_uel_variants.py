"""UEL award options and requirement ownership, independent of transport."""
import asyncio

import pytest
from bs4 import BeautifulSoup

from app.services.scraper.extractors.uel_variants import (
    is_uel_course_url,
    parse_uel_variants,
    scope_uel_variant,
    uel_source_url,
    uel_variant_key,
)
from app.services.scraper.pipelines.single_course import _extract_uel_variant


URL = "https://www.uel.ac.uk/postgraduate/courses/ma-theatre-directing"


def option(label, title, duration, fee="£16620 per year", audience="International Applicant",
           attendance="Full time"):
    return f"""
    <div class="outer-div"><div class="tab-degree-type">
      <h3 class="degree-type">{label}</h3></div></div>
    <div class="course-option-details">
      <div class="course-option-details-item-wrapper">
        <h4 class="course-route">{title}</h4>
        <ul class="fee-details-div">
          <li><span class="application-type">{audience}</span></li>
          <li><span class="attendance-type">{attendance}, </span>
              <span class="attendance-type-yr">{duration}</span></li>
          <li><span class="fee-type">{fee}</span></li>
        </ul>
      </div>
    </div>"""


def modal(label, key, score, band):
    return f"""
    <button aria-label="Full entry requirements for {label}"
      data-modal-id="{key}">Full entry requirements</button>
    <dialog id="{key}" class="modal-entry">
      <h2>English Language requirements</h2>
      <p>IELTS overall {score} with a minimum of {band} in each component.</p>
    </dialog>"""


def page(groups, requirements="", title="Theatre Directing MA"):
    return f"""<html><head><title>{title}</title>
      <meta name="description" content="Develop advanced theatre practice."></head>
      <body><h1>{title}</h1>{groups}{requirements}</body></html>"""


def tab(options, date="September 2026"):
    return f'<div class="course-options-content-div" aria-label="{date}">{options}</div>'


def extract(variant):
    return asyncio.run(_extract_uel_variant(variant, country="United Kingdom"))["payload"]


def test_ma_mfa_names_and_different_ielts_are_bound_to_their_own_modals():
    html = page(tab(
        option("MA", "Theatre Directing MA", "1 year")
        + option("MFA", "Theatre Directing MA", "2 years", "£18000 per year")
    ), modal("MA", "ma", 6.0, 5.5) + modal("MFA", "mfa", 7.0, 6.5))
    variants = parse_uel_variants(html, URL)
    assert [v.name for v in variants] == ["Theatre Directing MA", "Theatre Directing MFA"]
    first, second = map(extract, variants)
    assert first["ielts_overall"] == 6
    assert second["ielts_overall"] == 7
    assert first["ielts_listening"] == 5.5
    assert second["ielts_listening"] == 6.5
    assert first["duration"] == 1
    assert second["duration"] == 2
    assert first["international_fee"] == 16620
    assert second["international_fee"] == 18000
    assert len({v.url for v in variants}) == 2
    assert "7.0" not in variants[0].html
    assert "6.0" not in variants[1].html


def test_foundation_title_not_duplicated_and_domestic_routes_not_international():
    title = "Sports Coaching and Performance (West Ham United Foundation) BSc (Hons)"
    groups = (
        option("Degree", title, "3 years", "£9790 per year", "Home Applicant")
        + option("Degree with foundation year", title, "4 years", "£9790 per year", "Home Applicant")
    )
    variants = parse_uel_variants(page(tab(groups), title=title),
                                  "https://www.uel.ac.uk/undergraduate/courses/sports")
    assert [v.name for v in variants] == [title, title + " with foundation year"]
    assert all(not v.international for v in variants)
    assert [extract(v)["duration"] for v in variants] == [3, 4]
    assert all(extract(v).get("international_fee") is None for v in variants)


def test_repeated_intake_tabs_do_not_create_duplicate_awards_or_domestic_intakes():
    title = "Mechanical Engineering MSc"
    opts = option("MSc", title, "1/2 years") + option("MSc with Placement Year", title, "1/2 years")
    domestic = option("MSc", title, "1 year", audience="Home Applicant")
    variants = parse_uel_variants(page(
        tab(opts) + tab(opts, "January 2027") + tab(domestic, "May 2027"),
        title=title,
    ), "https://www.uel.ac.uk/postgraduate/courses/msc-mechanical-engineering")
    assert len(variants) == 2
    assert [v.name for v in variants] == [title, title + " with Placement Year"]
    assert [extract(v)["duration"] for v in variants] == [1, 2]
    assert all(set(extract(v)["intake_months"]) == {"January", "September"} for v in variants)


def test_part_time_international_does_not_borrow_home_fulltime():
    title = "Theatre Directing MA"
    rows = option("MA", title, "5 years", attendance="Part time")
    rows += option("MA", title, "1 year", audience="Home Applicant")
    rows += option("MFA", title, "2 years")
    variants = parse_uel_variants(page(tab(rows)), URL)
    assert variants[0].international
    assert not variants[0].full_time
    assert extract(variants[0])["study_load"] == "Part Time"


def test_missing_modal_cannot_borrow_sibling_or_global_english_scores():
    html = page(tab(option("MA", "Theatre Directing MA", "1 year")
                    + option("MFA", "Theatre Directing MA", "2 years")),
                modal("MA", "ma", 7, 6) + "<footer>IELTS 9.0</footer>")
    second = parse_uel_variants(html, URL)[1]
    assert 'data-uel-requirements="missing"' in second.html
    assert not extract(second).get("ielts_overall")
    assert "IELTS" not in second.html


def test_single_award_many_applicants_and_intakes_is_not_fanned_out():
    opts = option("MA", "Theatre Directing MA", "1 year")
    assert parse_uel_variants(page(tab(opts) + tab(opts, "January 2027")), URL) == []


def test_selector_identity_and_unknown_selection_fail_closed():
    html = page(tab(option("MA", "Theatre Directing MA", "1 year")
                    + option("MFA", "Theatre Directing MA", "2 years")))
    variant = parse_uel_variants(html, URL + "?language=en")[1]
    assert uel_variant_key(variant.url) == "mfa"
    assert uel_source_url(variant.url) == URL + "?language=en"
    assert scope_uel_variant(html, variant.url) == variant.html
    with pytest.raises(ValueError):
        scope_uel_variant(html, URL + "?uel_variant=missing")
    with pytest.raises(ValueError):
        uel_variant_key(URL + "?uel_variant=ma&uel_variant=mfa")
    assert not is_uel_course_url("https://uel.ac.uk.evil.test/postgraduate/courses/example")
    assert not parse_uel_variants(html, "https://example.edu/course")


def test_fee_second_year_placement_charge_is_not_tuition():
    title = "Mechanical Engineering MSc"
    fee = "£17220 per year. Year 2 Industrial Placement Fee - £3,500"
    html = page(tab(option("MSc", title, "1/2 years", fee)
                    + option("MSc with Placement Year", title, "1/2 years", fee)),
                title=title)
    assert [extract(v)["international_fee"] for v in parse_uel_variants(html, URL)] == [17220, 17220]


def test_matched_dialog_is_promoted_out_of_hidden_dialog_container():
    html = page(tab(option("MA", "Theatre Directing MA", "1 year")
                    + option("MFA", "Theatre Directing MA", "2 years")),
                modal("MA", "ma", 6, 5.5))
    scoped = BeautifulSoup(parse_uel_variants(html, URL)[0].html, "html.parser")
    assert not scoped.select("dialog")
    assert "IELTS overall 6" in scoped.get_text()


def test_foundation_nested_chooser_binds_distinct_requirements_not_accordion_order():
    title = "Sports Coaching BSc (Hons)"
    options = option("Degree", title, "3 years") + option("Degree with foundation year", title, "4 years")
    requirements = """
      <dialog id="entry-requirements-1">
        <button data-label="Degree (including contextual offer)" data-details-screen="degree"></button>
        <button data-label="Degree with foundation year" data-details-screen="foundation"></button>
        <div class="entry-requirements-details-screen" id="foundation"
          data-label="Degree with foundation year" style="display: none;">
          <p>IELTS overall 5.5 with no component below 5.0.</p>
        </div>
        <div class="entry-requirements-details-screen" id="degree"
          data-label="Degree (including contextual offer)" style="display: none;">
          <p>IELTS overall 6.5 with no component below 6.0.</p>
        </div>
      </dialog>"""
    variants = parse_uel_variants(page(tab(options), requirements, title), URL)
    assert extract(variants[0])["ielts_overall"] == 6.5
    assert extract(variants[1])["ielts_overall"] == 5.5
    assert extract(variants[0])["ielts_listening"] == 6.0
    assert extract(variants[1])["ielts_listening"] == 5.0
    assert all("display: none" not in v.html for v in variants)


@pytest.mark.parametrize("missing", ["application-type", "attendance-type"])
def test_unknown_option_evidence_is_not_fabricated_as_ineligible(missing):
    opts = option("MA", "Theatre Directing MA", "1 year")
    opts += option("MFA", "Theatre Directing MA", "2 years")
    html = page(tab(opts)).replace(f'class="{missing}"', 'class="changed-template"')
    with pytest.raises(ValueError, match="unknown"):
        parse_uel_variants(html, URL)


def test_incomplete_multiaward_template_never_falls_back_to_shared_extraction():
    opts = option("MA", "Theatre Directing MA", "1 year")
    opts += '<div><h3 class="degree-type">MFA</h3></div><div class="course-option-details"></div>'
    with pytest.raises(ValueError, match="no recognized option rows"):
        parse_uel_variants(page(tab(opts)), URL)


def test_selected_route_survives_removal_of_sibling_from_page():
    html = page(tab(option("MA", "Theatre Directing MA", "1 year")), modal("MA", "ma", 6, 5.5))
    assert parse_uel_variants(html, URL) == []
    selected = parse_uel_variants(html, URL + "?uel_variant=ma")
    assert len(selected) == 1
    assert selected[0].key == "ma"
    assert extract(selected[0])["ielts_overall"] == 6
    assert scope_uel_variant(html, selected[0].url) == selected[0].html


def test_uel_grouped_ielts_skills_keep_both_published_minima():
    html = page(tab(option("MA", "Theatre Directing MA", "1 year")
                    + option("MFA", "Theatre Directing MA", "2 years")),
                modal("MA", "ma", 6, 5.5).replace(
                    "IELTS overall 6 with a minimum of 5.5 in each component.",
                    "Overall IELTS 6.0 with minimum 6.0 in writing and speaking, "
                    "and 5.5 in listening and reading(or recognised equivalent)",
                ) + modal("MFA", "mfa", 7, 6.5))
    first, second = map(extract, parse_uel_variants(html, URL))
    assert first["ielts_overall"] == 6
    assert first["ielts_writing"] == first["ielts_speaking"] == 6
    assert first["ielts_listening"] == first["ielts_reading"] == 5.5
    assert second["ielts_overall"] == 7
    assert second["ielts_listening"] == second["ielts_reading"] == 6.5