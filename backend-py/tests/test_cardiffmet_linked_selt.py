import pytest
from app.services.scraper.extractors import cardiffmet_facts as c

URL = c.BASE + "/courses/postgraduate/msc-computing/"
HUB = f'<main><h1>English Language Requirements for International Applicants</h1><a href="{c.SELT}">Accepted tests</a></main>'
TABLE = """<main><h1>Accepted Secure English Language Tests (SELTs)</h1><table>
<tr><th>Qualification</th><th>Courses with 6.0 IELTS Requirements</th><th>Courses with 6.5 IELTS Requirements</th></tr>
<tr><td>IELTS Academic <p>We accept IELTS One Skill Retake</p></td>
<td>Overall score: 6.0 (with no component below 5.5)</td>
<td>6.5 overall with 6.5 in both reading and writing, and a minimum of 6.0 in the remaining components</td></tr>
</table></main>"""


def apply(score=6.0, location="Llandaff Campus", referral=True, payload=None, sources=None, extra=""):
    html = f"""<main><h1>MSc Computing</h1><div class="block-12__item">
    <h4>Location</h4><p class="block-12__item-description">{location}</p></div>
    <h3>English Language Requirements</h3>
    {f'<a href="{c.ENGLISH}">English Language Requirements</a>' if referral else ''}
    <h3>International Applicants</h3><p>Applicants require an IELTS {score} standard or equivalent.</p>
    {extra}</main>"""
    payload = dict(payload or {})
    evidence = []
    c.apply_authority(html, URL, payload, evidence, {c.ENGLISH: HUB, c.SELT: TABLE} if sources is None else sources)
    return payload, evidence


@pytest.mark.parametrize("score,values", [
    (6.0, (6.0, 5.5, 5.5, 5.5, 5.5)),
    (6.5, (6.5, 6.0, 6.5, 6.5, 6.0)),
])
def test_course_score_selects_its_own_table_column(score, values):
    p, e = apply(score)
    assert tuple(p[field] for field in c.IELTS) == values
    assert all(row["source_url"] == c.SELT for row in e)
    assert all(URL in row["snippet"] and c.ENGLISH in row["snippet"] for row in e)


@pytest.mark.parametrize("kwargs", [
    {"location": "Partner Institution"},
    {"location": ""}, {"score": 7.5},
    {"sources": {c.SELT: TABLE}},
    {"sources": {c.ENGLISH: HUB}},
    {"sources": {c.ENGLISH: HUB.replace(c.SELT, "/unrelated/"), c.SELT: TABLE}},
    {"extra": "<p>Another route requires IELTS 6.5.</p>"},
    {"extra": "<h3>Entry requirements</h3><p>This programme requires IELTS 7.0.</p>"},
    {"extra": "<h3>Entry requirements</h3><p>IELTS 6.0 overall, writing 6.0.</p>"},
    {"extra": "<table><tr><td>IELTS</td><td>7.5 for a selected route</td></tr></table>"},
    {"payload": {"ielts_overall": 7.5}},
    {"payload": {"ielts_writing": 7.0}},
])
def test_missing_or_conflicting_applicability_never_gets_general_bands(kwargs):
    before = kwargs.get("payload", {})
    assert apply(**kwargs)[0] == before


def test_existing_matching_scores_are_preserved_while_missing_bands_are_filled():
    p, e = apply(payload={"ielts_overall": 6.0, "ielts_reading": 5.5})
    assert p["ielts_listening"] == 5.5 and p["ielts_reading"] == 5.5
    assert {row["field_key"] for row in e} == {"ielts_listening", "ielts_writing", "ielts_speaking"}


def test_other_test_scores_are_never_used_as_ielts():
    sources = {c.ENGLISH: HUB, c.SELT: TABLE.replace("IELTS Academic", "PTE Academic")}
    assert not apply(sources=sources)[0]


@pytest.mark.parametrize("location", ["Online", "Llandaff Campus, Cyncoed Campus"])
def test_explicit_course_score_can_use_linked_tests_at_known_delivery_locations(location):
    p, _ = apply(location=location)
    assert p["ielts_listening"] == 5.5


def test_published_score_bracket_can_match_a_current_course_without_an_in_page_link():
    p, _ = apply(referral=False)
    assert tuple(p[field] for field in c.IELTS) == (6.0, 5.5, 5.5, 5.5, 5.5)


def test_stored_overall_alone_does_not_establish_the_course_score_bracket():
    html = """<main><h1>Computing - MSc</h1><div class="block-12__item"><h4>Location</h4>
    <p class="block-12__item-description">Llandaff Campus</p></div><p>See English guidance.</p></main>"""
    p, e = {"ielts_overall": 6.0}, []
    c.apply_authority(html, URL, p, e, {c.ENGLISH: HUB, c.SELT: TABLE})
    assert p == {"ielts_overall": 6.0} and not e


@pytest.mark.parametrize("minimum,matched", [(6.5, True), (7.0, False)])
def test_research_reading_writing_constraints_must_agree_with_linked_table(minimum, matched):
    extra = f"""<p>Students need to provide evidence of fluency to at least an IELTS 6.5 standard
    (with minimum {minimum} in the Reading and Writing components) or equivalent.</p>"""
    p, _ = apply(score=6.5, extra=extra)
    assert bool(p) == matched
    if matched:
        assert p["ielts_reading"] == p["ielts_writing"] == 6.5
        assert p["ielts_listening"] == p["ielts_speaking"] == 6.0


def test_partial_research_rule_does_not_promote_reading_writing_minimum_to_other_skills():
    extra = """<p>Students need to provide evidence of fluency to at least an IELTS 6.5 standard
    (with minimum 6.5 in the Reading and Writing components) or equivalent.</p>"""
    p, e = apply(score=6.5, extra=extra, payload=dict.fromkeys(c.IELTS, 6.5))
    assert tuple(p[field] for field in c.IELTS) == (6.5, 6.0, 6.5, 6.5, 6.0)
    assert {row["field_key"] for row in e} == {"ielts_listening", "ielts_speaking"}
    for protected in ({"ielts_overall": 7.5}, {"ielts_reading": 7.0}):
        assert apply(score=6.5, extra=extra, payload=protected)[0] == protected


@pytest.mark.parametrize("prefix", ["<strong>International Applicants</strong><br>",
                                  "<strong>International Applicants<br></strong><br>"])
def test_same_paragraph_international_label_supports_a_published_score_bracket(prefix):
    html = f"""<main><h1>International Banking and Finance - BSc</h1><div class="block-12__item">
    <h4>Location</h4><p class="block-12__item-description">Llandaff Campus</p></div>
    <p>{prefix}Students whose first language is not English will need to provide evidence
    of fluency to at least an IELTS 6.0 standard or equivalent.</p></main>"""
    p = {}
    c.apply_authority(html, URL, p, [], {c.ENGLISH: HUB, c.SELT: TABLE})
    assert tuple(p[field] for field in c.IELTS) == (6.0, 5.5, 5.5, 5.5, 5.5)


def test_paragraph_heading_can_introduce_a_course_owned_international_requirement():
    html = f"""<main><h1>Professional Practice - MSc</h1><div class="block-12__item">
    <h4>Location</h4><p class="block-12__item-description">Online</p></div>
    <a href="{c.ENGLISH}">English Language Requirements</a><h3>About the Course</h3>
    <p><strong>International Applicants:</strong></p>
    <p>Students need to provide evidence of fluency to at least an IELTS 6.0 standard or equivalent.</p></main>"""
    p, e = {}, []
    c.apply_authority(html, URL, p, e, {c.ENGLISH: HUB, c.SELT: TABLE})
    assert p["ielts_listening"] == 5.5


@pytest.mark.parametrize("requirement", [
    "Applicants may require IELTS 6.0.",
    "Applicants require IELTS 6.0 for one route and 6.5 for another.",
    "Applicants require IELTS 6.0 overall, 5.5 minimum individually.",
])
def test_conditional_or_unparsed_component_scores_are_not_generalised(requirement):
    html = f"""<main><h1>MSc Computing</h1><div class="block-12__item">
    <h4>Location</h4><p class="block-12__item-description">Llandaff Campus</p></div>
    <a href="{c.ENGLISH}">English Language Requirements</a>
    <h3>International Applicants</h3><p>{requirement}</p></main>"""
    p, e = {}, []
    c.apply_authority(html, URL, p, e, {c.ENGLISH: HUB, c.SELT: TABLE})
    assert p == {} and e == []
