from types import SimpleNamespace

import pytest

from app.services.english_applicability import applicability, category, quote_page, score_binding, uploaded_course_evidence
from app.services.assistant_document_review import requested, validate_changes


def sample(name="Bachelor of Business", level="Undergraduate", scope=None):
    row = SimpleNamespace(course_name=name, degree_level=level)
    quote = scope or "All undergraduate courses require IELTS overall 6.5 with no component below 5.5."
    identity = f"{name} is an undergraduate course."
    source = {"verified": True, "sourceType": "pdf", "url": "https://uni.ac.uk/english.pdf",
              "text": quote, "_pages": [{"page": 2, "text": quote}]}
    course = {"verified": True, "url": "https://uni.ac.uk/course", "text": identity}
    candidate = {"applicability": {"guidanceQuote": quote, "courseQuote": identity, "page": 2}}
    return row, candidate, source, course


def test_explicit_level_scope_with_named_official_course():
    result = applicability(*sample())
    assert result["applicability"] == "verified"
    assert result["category"] == "undergraduate"
    assert result["evidence"][0]["page"] == 2
    assert len(result["evidence"]) == 2


@pytest.mark.parametrize("name,level,expected", [
    ("Bachelor of Nursing", "Undergraduate", "health"),
    ("MRes Business", "Postgraduate", "postgraduate_research"),
    ("Master of Business", "Postgraduate", "postgraduate_taught"),
    ("Bachelor of Business", "Undergraduate", "undergraduate"),
    ("Business", "", "unknown"),
])
def test_categories_are_conservative(name, level, expected):
    assert category(SimpleNamespace(course_name=name, degree_level=level)) == expected


@pytest.mark.parametrize("name,level", [
    ("Bachelor of Nursing", "Undergraduate"), ("MRes Business", "Postgraduate"),
])
def test_health_research_never_inherit_level_default(name, level):
    assert applicability(*sample(name, level))["applicability"] == "excluded"


def test_named_health_guidance_can_be_verified_without_exclusions():
    assert applicability(*sample("Bachelor of Nursing", "Undergraduate",
        "Bachelor of Nursing requires IELTS overall 7.0."))["applicability"] == "verified"


@pytest.mark.parametrize("mutate", [
    lambda r, c, s, p: s.update(verified=False),
    lambda r, c, s, p: p.update(verified=False),
    lambda r, c, s, p: s.update(unreadablePages=[3]),
    lambda r, c, s, p: c["applicability"].update(page=1),
    lambda r, c, s, p: c["applicability"].update(courseQuote="Invented course evidence"),
    lambda r, c, s, p: s.update(_pages=[]),
    lambda r, c, s, p: c.update(applicability="invalid"),
])
def test_unchecked_or_unproven_is_never_verified(mutate):
    args = sample()
    mutate(*args)
    assert applicability(*args)["applicability"] == "unverified"


def test_taught_and_research_are_not_interchangeable():
    args = sample("Master of Business", "Postgraduate",
                  "All postgraduate taught courses require IELTS overall 6.5.")
    args[3]["text"] = args[1]["applicability"]["courseQuote"] = "Master of Business is a postgraduate taught course."
    assert applicability(*args)["applicability"] == "verified"
    args[3]["text"] = args[1]["applicability"]["courseQuote"] = "Master of Business is a postgraduate course."
    assert applicability(*args)["applicability"] == "unverified"


def test_exclusion_is_not_positive_name_match():
    result = applicability(*sample(scope="All undergraduate courses except Bachelor of Business require IELTS overall 6.5."))
    assert result["applicability"] == "excluded"


def test_score_page_and_conflicting_course_facts():
    _, _, source, course = sample()
    candidate = {"changes": [{"field": "ielts_overall", "newValue": 6.5,
                             "quote": source["text"], "page": 2}]}
    assert validate_changes(candidate, source, course)
    candidate["changes"][0]["page"] = 1
    assert not validate_changes(candidate, source, course)
    candidate["changes"][0]["page"] = 2
    for value in (6.0, 7.0):
        course["facts"] = [{"field": "ielts_overall", "value": value}]
        assert not validate_changes(candidate, source, course)


def test_html_quote_has_no_invented_pdf_page():
    source = {"text": "English guidance for the named course."}
    assert quote_page(source, source["text"], None)
    assert not quote_page(source, source["text"], 1)


def test_report_requests_reach_course_matching():
    assert requested({"content": "Check English applicability for each course"})
    assert requested({"content": "Match English guidance to each course"})
    assert requested({"content": "Check https://uni.ac.uk/english.pdf"})


@pytest.mark.parametrize("name", ["Bachelor of Nursing", "Bachelor of Pharmacy"])
def test_general_score_cannot_be_borrowed_from_mixed_named_passage(name):
    row, candidate, source, _ = sample(name, "Undergraduate",
        f"All undergraduate courses require IELTS overall 6.5. {name} requires IELTS overall 7.0.")
    candidate["changes"] = [{"field": "ielts_overall", "newValue": 6.5,
                             "quote": source["text"], "page": 2}]
    assert not score_binding(row, candidate, source)
    candidate["changes"][0]["quote"] = "All undergraduate courses require IELTS overall 6.5."
    assert not score_binding(row, candidate, source)


def test_correct_named_clause_can_support_its_own_score():
    row, candidate, source, _ = sample("Bachelor of Nursing", "Undergraduate",
        "Bachelor of Nursing requires IELTS overall 7.0.")
    candidate["changes"] = [{"field": "ielts_overall", "newValue": 7.0,
                             "quote": source["text"], "page": 2}]
    source["_fullText"] = "All undergraduate courses require IELTS overall 6.5. " + source["text"]
    assert score_binding(row, candidate, source)
    source["_fullText"] += " Bachelor of Nursing requires IELTS overall 7.5."
    assert not score_binding(row, candidate, source)


@pytest.mark.parametrize("exception", [
    "Exceptions apply to Pharmacy courses, which require IELTS overall 7.0.",
    "Ceramics courses have different English requirements.",
    "Bachelor of Business requires IELTS overall 7.0.",
])
def test_exceptions_outside_the_chosen_quote_block_default(exception):
    args = sample()
    args[2]["_fullText"] = args[2]["text"] + " " + exception
    assert applicability(*args)["applicability"] in {"excluded", "conflict"}


def test_unlabelled_subject_requirement_elsewhere_cannot_inherit_default():
    row, candidate, source, _ = sample()
    source["_fullText"] = source["text"] + " Ceramics requires IELTS overall 7.0."
    candidate["changes"] = [{"field": "ielts_overall", "newValue": 6.5, "quote": source["text"], "page": 2}]
    assert not score_binding(row, candidate, source)


def test_uploaded_named_course_can_supply_evidence_without_live_website():
    row, candidate, source, _ = sample(scope="Bachelor of Business requires IELTS overall 6.5.")
    source.update(provenance="staff-uploaded-document", officialProvenanceVerified=False)
    course = uploaded_course_evidence(row, source)
    assert course and course["officialProvenanceVerified"] is False
    candidate["applicability"]["courseQuote"] = course["text"]
    result = applicability(row, candidate, source, course)
    assert result["applicability"] == "verified"
    assert "not independently verified" in result["reason"]


@pytest.mark.parametrize("quote", [
    "Bachelor of Business Administration requires IELTS overall 6.5.",
    "Not Bachelor of Business requires IELTS overall 6.5.",
])
def test_upload_cannot_invent_course_identity_or_borrow_general_scope(quote):
    row, candidate, source, _ = sample(scope=quote)
    source["provenance"] = "staff-uploaded-document"
    course = uploaded_course_evidence(row, source)
    if course:
        candidate["applicability"]["courseQuote"] = course["text"]
        # A name embedded in a different award or negative statement is not authority.
        assert applicability(row, candidate, source, course)["applicability"] != "verified"


@pytest.mark.parametrize("field", ["listening", "reading", "writing", "speaking"])
def test_each_individual_band_is_extracted_from_named_upload_evidence(field):
    quote = f"Bachelor of Business requires IELTS overall 6.5; {field}: 6.0."
    row, candidate, source, course = sample(scope=quote)
    candidate["changes"] = [{"field": f"ielts_{field}", "newValue": 6.0, "quote": quote, "page": 2}]
    assert validate_changes(candidate, source, course)
    assert score_binding(row, candidate, source)


@pytest.mark.parametrize("provenance", ["staff-uploaded-document", "official-document"])
@pytest.mark.parametrize("university", ["university-one.example", "university-two.example", "lsbu.ac.uk"])
def test_multi_level_pdf_matches_existing_records_without_live_pages(provenance, university):
    ug = "All undergraduate courses require IELTS overall 6.5 with no component below 5.5."
    pg = "All postgraduate taught courses require IELTS overall 7.0 with no component below 6.0."
    for name, level, quote, score, band in [
        ("Bachelor of Business", "Undergraduate", ug, 6.5, 5.5),
        ("Master of Business", "Master", pg, 7.0, 6.0),
    ]:
        row, candidate, source, _ = sample(name, level, quote)
        source.update(url=f"https://{university}/requirements.pdf", provenance=provenance,
                      _fullText=ug + "\n" + pg, _pages=[{"page": 2, "text": ug + "\n" + pg}])
        course = uploaded_course_evidence(row, source)
        assert course
        candidate["applicability"]["courseQuote"] = course["text"]
        candidate["changes"] = [
            {"field": "ielts_overall", "newValue": score, "quote": quote, "page": 2},
            *[{"field": f"ielts_{field}", "newValue": band, "quote": quote, "page": 2}
              for field in ["listening", "reading", "writing", "speaking"]],
        ]
        assert applicability(row, candidate, source, course)["applicability"] == "verified"
        assert len(validate_changes(candidate, source, course)) == 5
        assert score_binding(row, candidate, source)


def test_subject_scope_uses_record_subject_not_name_guess():
    row, candidate, source, _ = sample(scope="All engineering courses require IELTS overall 6.5.")
    row.category = "Engineering"
    course = uploaded_course_evidence(row, source)
    assert course
    candidate["applicability"]["courseQuote"] = course["text"]
    assert applicability(row, candidate, source, course)["applicability"] == "verified"
    row.category = "Business"
    assert uploaded_course_evidence(row, source) is None


@pytest.mark.parametrize("title", ["Accounting & Finance BSc (Hons)", "BSc (Hons) Accounting and Finance"])
def test_harmless_award_order_and_punctuation_differences(title):
    row, candidate, source, _ = sample(
        "Bachelor of Science (Honours) Accounting and Finance", "Bachelor",
        f"{title} requires IELTS overall 6.5.",
    )
    course = uploaded_course_evidence(row, source)
    assert course
    candidate["applicability"]["courseQuote"] = course["text"]
    candidate["changes"] = [{"field": "ielts_overall", "newValue": 6.5, "quote": source["text"], "page": 2}]
    assert applicability(row, candidate, source, course)["applicability"] == "verified"
    assert score_binding(row, candidate, source)
    row.course_name += " with Foundation Year"
    assert uploaded_course_evidence(row, source) is None


def test_unknown_level_and_protected_courses_never_use_record_default():
    for name, level in [("Business", "Postgraduate"), ("Bachelor of Nursing", "Undergraduate"),
                        ("MRes Business", "Master")]:
        row, _, source, _ = sample(name, level)
        assert uploaded_course_evidence(row, source) is None


def test_explicit_health_exception_does_not_block_unrelated_business_course():
    row, candidate, source, _ = sample()
    source["_fullText"] = source["text"] + "\nExceptions apply to health courses."
    course = uploaded_course_evidence(row, source)
    candidate["applicability"]["courseQuote"] = course["text"]
    assert applicability(row, candidate, source, course)["applicability"] == "verified"


def test_single_component_minimum_is_not_a_floor_for_other_bands():
    row, candidate, source, course = sample(scope="Bachelor of Business requires IELTS Reading minimum 6.0.")
    candidate["changes"] = [{"field": "ielts_speaking", "newValue": 6.0,
                              "quote": source["text"], "page": 2}]
    assert not validate_changes(candidate, source, course)
    assert not score_binding(row, candidate, source)
    candidate["changes"][0]["field"] = "ielts_reading"
    assert validate_changes(candidate, source, course)
    assert score_binding(row, candidate, source)
