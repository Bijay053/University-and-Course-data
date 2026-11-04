import pytest
import re

from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.extractors.uws_facts import (
    apply_course_authority, course_english, course_fee,
)

URL = "https://www.uws.ac.uk/study/undergraduate/undergraduate-course-search/music-technology/"
ENGLISH = """
<h3>UWS &amp; INTERNATIONAL ENGLISH LANGUAGE TEST SYSTEM (IELTS)</h3>
<h4>Undergraduate degrees and postgraduate taught degrees</h4>
<ul><li>normally an overall IELTS score of 6.0 (or equivalent),
with a score of no lower than 5.5 in any of the four elements</li></ul>
<h4>Postgraduate research degrees</h4>
<ul><li>an overall IELTS score of 6.5 (or equivalent),
with a score of no lower than 6.0 in any of the four elements</li></ul>
<h3>Pre-sessional courses</h3><p>IELTS 5.0 for a preparatory course</p>
"""


def fee_panel(year, value, international=True):
    return f"""<div class="text"><h3>Tuition fees for this course {year}/{str(year+1)[-2:]}</h3>
    <div class="fee-block"><h4>All other UK</h4><p>£9,790 per year of study</p></div>
    {f'<div class="fee-block"><h4>International (all other EU plus Non-EU)</h4><p>£{value} per year of study</p></div>' if international else ''}
    </div>"""


def test_latest_international_fee_not_domestic_or_module():
    html = fee_panel(2025, "18,000") + fee_panel(2026, "18,900")
    html += '<h3>Modular Tuition Fees 2026/27</h3><div class="fee-block"><h4>International</h4><p>£3,150 per module</p></div>'
    known, result = course_fee(html, URL)
    assert known and result.value == 18900
    assert result.normalized == {"international_fee": 18900, "currency": "GBP", "fee_term": "Annual", "fee_year": 2026}


def test_latest_unpublished_does_not_borrow_uk_or_previous_year():
    assert course_fee(fee_panel(2025, "18,000") + fee_panel(2026, "", False), URL) == (True, None)


def test_postgraduate_total_is_not_relabelled_as_annual():
    html = fee_panel(2026, "18,900").replace("per year of study", "Total cost of tuition")
    known, fee = course_fee(html, URL.replace("undergraduate", "postgraduate"))
    assert known and fee.normalized["fee_term"] == "Full Course"


def test_module_only_price_is_not_a_whole_award_or_annual_fee():
    html = '<h3>Modular Tuition Fees (20-credit points) 2026-27</h3><div class="fee-block"><h4>International</h4><p>£3,150 per module</p></div>'
    assert course_fee(html, URL) == (True, None)


def test_higher_overall_wording_is_also_not_downgraded():
    known, english = course_english("<h3>Course admission</h3><p>An overall IELTS score of 7.0 is required.</p>" + ENGLISH, URL)
    assert known and english[0].value == 7
    assert english[0].normalized["ielts_reading"] is None


@pytest.mark.parametrize("slug,overall,floor", [("music-technology", 6.0, 5.5), ("psychology-mres", 6.5, 6.0)])
def test_english_profile_is_not_pre_sessional_score(slug, overall, floor):
    url = URL.rsplit("music-technology/", 1)[0] + slug + "/"
    known, english = course_english(ENGLISH, url)
    assert known and english[0].value == overall
    assert english[0].normalized["ielts_reading"] == floor


def test_course_specific_higher_requirement_is_not_downgraded():
    known, english = course_english('<p>Applicants require IELTS 7.0 for admission.</p>' + ENGLISH, URL)
    assert known and english[0].value == 7.0


def test_direct_research_admission_profile():
    known, english = course_english(
        "<h1>Master of Research (MRes)</h1><p>Applicants must demonstrate English proficiency: IELTS score of 6.5 overall with no individual component below 6.0.</p>",
        URL,
    )
    assert known and english[0].value == 6.5
    assert english[0].normalized["ielts_listening"] == 6


def test_social_work_course_specific_floor_wins_over_template():
    known, english = course_english("<h3>Entry requirements</h3><p>International students must also have attained IELTS 7, as a minimum across all aspects.</p>" + ENGLISH, URL)
    assert known and english[0].value == 7
    assert english[0].normalized["ielts_reading"] == 7


def test_official_central_taught_profile_wording():
    html = ENGLISH.replace(
        "Undergraduate degrees and postgraduate taught degrees",
        "For all undergraduate bachelor's degrees and taught postgraduate degrees",
    )
    known, english = course_english(html, "https://www.uws.ac.uk/international/english-language-requirements/")
    assert known and english[0].value == 6.0


def test_research_identity_in_heading_does_not_need_mres_in_url():
    known, english = course_english("<h1>MRes Health Sciences</h1>" + ENGLISH, URL)
    assert known and english[0].value == 6.5


def test_late_authority_replaces_ai_domestic_and_preparatory_values():
    payload = {"international_fee": 9790, "ielts_overall": 5.0}
    evidence = []
    apply_course_authority(fee_panel(2026, "18,900") + ENGLISH, URL, payload, evidence)
    assert payload["international_fee"] == 18900
    assert payload["ielts_overall"] == 6
    assert payload["ielts_writing"] == 5.5
    assert len(evidence) == 2


def test_other_hosts_and_listing_pages_are_untouched():
    for url in ["https://unrelated.test" + URL.split(".ac.uk")[1], URL.rsplit("music-technology/", 1)[0]]:
        assert course_fee(fee_panel(2026, "18,900"), url) == (False, None)
        assert course_english(ENGLISH, url) == (False, [])


def test_verified_uws_catalogue_and_qualificationless_headings_survive_stale_admin():
    cfg = load_uni_config(
        slug="uws", name="University of the West of Scotland", university_id=109,
        scrape_url="https://www.uws.ac.uk/", create_missing_stub=False,
        db_scrape_config={"admin_config": {"discovery": {"generic_search_api": None,
                         "always_sitemap_supplement": True, "use_wayback": True,
                         "block_url_patterns": ["^https://www.uws.ac.uk/study/postgraduate.*"]},
                         "extraction": {"staging": {"skip_degree_qualifier_check": False}}}},
    )
    assert cfg.discovery.generic_search_api.body["CourseType"] == ""
    assert cfg.discovery.generic_search_api.body["PageSize"] == 500
    assert cfg.extraction.staging.skip_degree_qualifier_check
    assert cfg.extraction.study_mode.suppress_nav_rule
    assert cfg.discovery.always_sitemap_supplement is False
    assert cfg.discovery.use_wayback is False
    pg = "https://www.uws.ac.uk/study/postgraduate/postgraduate-course-search/advanced-computing/"
    assert any(re.search(p, pg) for p in cfg.discovery.allow_url_patterns)
    assert not any(re.search(p, pg) for p in cfg.discovery.block_url_patterns)
