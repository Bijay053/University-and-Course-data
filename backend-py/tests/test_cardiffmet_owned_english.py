import pytest
from app.services.scraper.extractors import cardiffmet_facts as c

URL = c.BASE + "/courses/postgraduate/pgce-primary/"
PGCE = ("For international applicants, whose first language is not English, a minimum "
        "IELTS score of 7.5 overall with no sub score lower than 7.0 (or equivalent qualification) "
        "will be required for admission to the programme.")
NUTRITION = ("Students whose first language is not English will need to provide evidence of "
             "fluency to at least an academic IELTS 6.0 standard with no sub score lower than 5.5, or equivalent.")
DENTAL = ("Students whose first language is not English will need to provide evidence of fluency "
          "at least to an IELTS 6.5 overall, with at least 6.0 in all elements, or equivalent.")


def run(text, *, location="Cyncoed Campus", extra="", title="Primary with QTS - PGCE", payload=None):
    html = f"""<main><h1>{title}</h1><div class="block-12__item"><h4>Location</h4>
    <p class="block-12__item-description">{location}</p></div><h3>International Applicants</h3>
    <p>{text}</p>{extra}</main>"""
    payload, evidence = dict(payload or {}), []
    c.apply_authority(html, URL, payload, evidence)
    return payload, evidence


@pytest.mark.parametrize("text,overall,minimum", [(PGCE, 7.5, 7.0), (NUTRITION, 6.0, 5.5), (DENTAL, 6.5, 6.0)])
def test_exact_course_owned_overall_and_uniform_bands(text, overall, minimum):
    p, e = run(text)
    assert p["ielts_overall"] == overall
    assert all(p[field] == minimum for field in c.IELTS[1:])
    assert all(row["source_url"] == URL and text in row["snippet"] for row in e)


def test_current_direct_requirement_supersedes_incorrect_extraction_with_evidence():
    p, _ = run(PGCE, payload={"ielts_overall": 6.0, "ielts_writing": 5.5})
    assert p["ielts_overall"] == 7.5 and p["ielts_writing"] == 7.0


def test_requirement_in_entry_list_preserves_all_five_values():
    html = f"""<main><h1>Psychology and Criminology - BSc</h1><div class="block-12__item">
    <h4>Location</h4><p class="block-12__item-description">Llandaff Campus, Cyncoed Campus</p></div>
    <h3>Entry Requirements</h3><ul><li>English Language Requirement:
    Academic IELTS 6.0 overall with at least 5.5 in all elements, or equivalent.</li></ul></main>"""
    p, e = {}, []
    c.apply_authority(html, URL, p, e)
    assert tuple(p[field] for field in c.IELTS) == (6.0, 5.5, 5.5, 5.5, 5.5)


def test_legacy_layout_table_is_not_an_unparsed_score_table():
    html = f"""<main><h1>Primary with QTS - PGCE</h1><div class="block-12__item">
    <h4>Location</h4><p class="block-12__item-description">Cyncoed Campus</p></div>
    <table><tr><td><h3>International Applicants</h3><p>{PGCE}</p></td></tr></table></main>"""
    p, e = {}, []
    c.apply_authority(html, URL, p, e)
    assert p["ielts_overall"] == 7.5 and p["ielts_reading"] == 7.0


@pytest.mark.parametrize("kwargs", [
    {"location": "Partner Institution"},
    {"extra": "<p>IELTS 8.0 is required for this option.</p>"},
    {"extra": f"<p>{NUTRITION}</p>"},
    {"extra": "<table><tr><td>IELTS</td><td>8.0 for the clinical route</td></tr></table>"},
    {"extra": "<p>IELTS 7.5 overall, with one band at 6.5 and the other bands at 7.0.</p>"},
])
def test_partner_or_conflicting_options_are_never_combined(kwargs):
    assert run(PGCE, **kwargs)[0] == {}


def test_pathway_specific_uniform_scores_are_not_flattened():
    assert run("Dental Technology pathway only: " + DENTAL)[0] == {}


@pytest.mark.parametrize("location,valid", [
    ("Llandaff Campus, Cyncoed Campus", True),
    ("Llandaff Campus, Partner Institution", False),
    ("Llandaff Campus or Bridgend College", False),
    ("Llandaff Campus, Online", False),
])
def test_shared_campus_prices_require_every_location_to_be_supported(location, valid):
    assert c._verified_location(location) is valid


def test_primary_pgce_suffix_is_the_award_not_a_different_masters():
    assert c._fee_group("Primary (3-11 Age Range) with QTS - PGCE") == "PGCE"
    assert c._fee_group("Education with PGCE equivalency - MA") == "Postgraduate Masters Degree"


@pytest.mark.parametrize("prefix", ["", "<strong>International Applicants</strong><br>",
                                  "<span><strong>International Applicants<br></strong></span>"])
def test_self_labelled_first_language_requirement_does_not_need_an_external_heading(prefix):
    html = f"""<main><h1>Education - MA</h1><div class="block-12__item"><h4>Location</h4>
    <p class="block-12__item-description">Cyncoed Campus</p></div>
    <h3>Staff Expertise</h3><p>{prefix}{NUTRITION}</p></main>"""
    p, e = {}, []
    c.apply_authority(html, URL, p, e)
    assert tuple(p[field] for field in c.IELTS) == (6.0, 5.5, 5.5, 5.5, 5.5)


def test_explicit_two_skill_pairs_preserve_their_different_minimums():
    text = ("Students whose first language is not English will need to provide evidence of fluency "
            "to at least an academic IELTS 6.5 overall with 6.5 in writing and reading, "
            "and 6.0 in speaking and listening.")
    p, _ = run(text, payload={"ielts_speaking": 5.5})
    assert tuple(p[field] for field in c.IELTS) == (6.5, 6.0, 6.5, 6.5, 6.0)
    assert run(text.replace("speaking and listening", "writing and listening"))[0] == {}


def test_labelled_social_work_entry_requirement_is_not_a_campus_fee_default():
    html = """<main><h1>Social Work - BSc</h1><div class="block-12__item"><h4>Location</h4>
    <p class="block-12__item-description">Llandaff Campus or Bridgend College</p></div>
    <ul><li>English Language Requirement: Academic IELTS 7.0 overall with at least
    6.5 in all elements, or equivalent.</li></ul></main>"""
    p, _ = {}, []
    c.apply_authority(html, URL, p, [])
    assert tuple(p[field] for field in c.IELTS) == (7.0, 6.5, 6.5, 6.5, 6.5)
    assert "international_fee" not in p


def test_related_card_requirement_is_not_the_current_courses_admission_rule():
    html = f"""<main><h1>Education - MA</h1><div class="block-12__item"><h4>Location</h4>
    <p class="block-12__item-description">Cyncoed Campus</p></div>
    <div class="related-courses"><h3>International Applicants</h3><p>{NUTRITION}</p></div></main>"""
    p = {}
    c.apply_authority(html, URL, p, [])
    assert not p
