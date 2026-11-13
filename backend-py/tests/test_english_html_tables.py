from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from app.services.english_html_tables import profiles, candidate, checked_profile, SCHEMA
from app.services.english_applicability import applicability, score_binding
from app.services.assistant_document_review import validate_changes
from app.services.document_ielts_matching import identity_matches

HEADER = "<tr><th>School / Programme</th><th>Reading</th><th>Writing</th><th>Listening</th><th>Speaking</th><th>Overall</th></tr>"
ROW = "<tr><th>School of Law and Social Sciences</th><td>6.5</td><td>6.5</td><td>6.0</td><td>6.0</td><td>6.5</td></tr>"


def fixture():
    html = ("<dl><dt>Main Entry IELTS Minimum Band Scores (Postgraduate Research)</dt><dd>"
            "<p>Postgraduate Research Programmes</p><table>" + HEADER + ROW + "</table></dd></dl>")
    source = {"verified": True, "sourceType": "html", "url": "https://uni.example/english",
              "_fullText": BeautifulSoup(html, "html.parser").get_text(" ", strip=True),
              "_englishTablesSchema": SCHEMA, "_englishTables": profiles(html)}
    row = SimpleNamespace(id=1, course_name="PhD - Law and Social Sciences", degree_level="Doctorate")
    course = {"verified": True, "courseTitle": row.course_name, "url": "https://uni.example/research/law",
              "_fullText": row.course_name + ". This research programme includes several specialist fields.", "facts": []}
    return html, source, row, course


def test_named_research_programme_keeps_five_column_meanings_through_all_validators():
    _, source, row, course = fixture()
    proposal = candidate(row, source, course)
    assert proposal
    assert applicability(row, proposal, source, course)["applicability"] == "verified"
    assert score_binding(row, proposal, source)
    assert len(validate_changes(proposal, source, course)) == 5
    assert {c["field"]: c["newValue"] for c in proposal["changes"]} == {
        "ielts_overall": 6.5, "ielts_reading": 6.5, "ielts_writing": 6.5,
        "ielts_listening": 6.0, "ielts_speaking": 6.0,
    }


@pytest.mark.parametrize("label", [
    "PhD Physics", "Bachelor of Law and Social Sciences", "PhD Law and Business",
])
def test_does_not_assign_school_scores_to_unrelated_programmes(label):
    _, source, row, course = fixture()
    row.course_name = label
    course.update(courseTitle=label, _fullText=label)
    assert candidate(row, source, course) is None


@pytest.mark.parametrize("mutation", [
    lambda s, r, p: s.update(verified=False),
    lambda s, r, p: s.update(_englishTablesSchema=0),
    lambda s, r, p: r.update(verified=False),
    lambda s, r, p: r.update(provenance="existing-record-and-document"),
    lambda s, r, p: r.update(facts=[{"field": "ielts_overall", "value": 7.5}]),
    lambda s, r, p: r.update(facts=[{"field": "ielts_overall",
                                  "normalized": {"ielts_overall": 6.5, "ielts_reading": 7.0}}]),
    lambda s, r, p: s.update(_fullText="A page without that table"),
    lambda s, r, p: p["changes"][0].update(newValue=8.0),
    lambda s, r, p: p["changes"].pop(),
    lambda s, r, p: p["applicability"].update(tableId="invented"),
    lambda s, r, p: p["applicability"].update(courseQuote="Different course"),
])
def test_table_proposals_fail_closed_on_missing_authority_or_forged_values(mutation):
    _, source, row, course = fixture()
    proposal = candidate(row, source, course)
    mutation(source, course, proposal)
    assert checked_profile(row, proposal, source, course) is None


@pytest.mark.parametrize("replace", [
    lambda h: h.replace("IELTS", "TOEFL"),
    lambda h: h.replace("Postgraduate Research", "Unknown"),
    lambda h: h.replace("<td>6.5</td>", "<td>at most two bands at 5.5</td>"),
    lambda h: h.replace("<td>6.5</td>", "<td colspan='bad'>6.5</td>"),
    lambda h: h.replace("<table>", "<p>A mandatory in-sessional programme applies.</p><table>"),
    lambda h: h.replace("<table>", "<h3>TOEFL</h3><table>"),
])
def test_ambiguous_conditional_or_other_test_tables_are_not_numeric_ielts_defaults(replace):
    html, _, _, _ = fixture()
    assert profiles(replace(html)) == []


def test_official_programme_acronym_is_allowed_only_when_present_in_live_h1():
    html, _, row, course = fixture()
    html = html.replace("Postgraduate Research", "Postgraduate").replace(
        "School of Law and Social Sciences", "Health and Life Sciences - OSPAP")
    source = {"verified": True, "sourceType": "html", "url": "https://uni.example/english",
              "_fullText": BeautifulSoup(html, "html.parser").get_text(" ", strip=True),
              "_englishTablesSchema": SCHEMA, "_englishTables": profiles(html)}
    row.course_name = "Overseas Pharmacists Assessment Programme (OSPAP) PGDip"
    row.degree_level = "Graduate Diploma"
    course.update(courseTitle=row.course_name, _fullText=row.course_name)
    assert candidate(row, source, course)
    course.update(courseTitle="Pharmacy PGDip", _fullText="Pharmacy PGDip")
    assert candidate(row, source, course) is None


def test_award_omission_is_not_a_different_award_or_subject():
    assert identity_matches("Aerospace Engineering BEng (Hons)", "Aerospace Engineering")
    assert identity_matches("PhD - Law and Social Sciences", "Law and Social Sciences")
    assert not identity_matches("Aerospace Engineering BEng (Hons)", "Aerospace Engineering MEng (Hons)")
    assert not identity_matches("Aerospace Engineering BEng (Hons)", "Mechanical Engineering")


def test_identical_responsive_tables_are_one_profile_not_a_conflict():
    html, source, _, _ = fixture()
    assert profiles(html + html) == source["_englishTables"]
