from types import SimpleNamespace
from app.services.scraper.excelsia_source import facts, apply_authority
from app.services.scraper.requirement_status import build_requirement_status

URL = "https://excelsia.edu.au/study/business/bachelor-of-business-accounting/"


def page(title="Bachelor of Business (Accounting)", fee="$8,333 semester/$50,000 in total", audience="Domestic and International"):
    return f"""<html><title>{title} Degree in Australia - Excelsia University College</title>
    <main><h1>{title}</h1><section><h2>{title}</h2>
    <p>Duration: 3 years full-time/6 years part-time</p><p>Available to: {audience}</p>
    <p>IELTS: 6.0 with no band less than 6.0</p>
    <p>Tuition Fee: International students: {fee}, Domestic (FEE-HELP)</p>
    <h3>Graduate Diploma of Music</h3><p>IELTS: 9.0 with no band less than 9.0</p>
    </section></main></html>"""


def test_total_and_bands_are_course_owned_and_verifiable():
    payload, evidence = {"course_location": "On campus"}, []
    apply_authority(page(), URL, payload, evidence)
    assert payload["course_name"] == "Bachelor of Business (Accounting)"
    assert payload["international_fee"] == 50000 and payload["fee_term"] == "Full Course"
    assert payload["course_location"] is None
    for component in ("listening", "reading", "writing", "speaking"):
        assert payload["ielts_" + component] == 6
    for item in evidence:
        item["decision_status"] = "selected"
    result = build_requirement_status(SimpleNamespace(**payload), evidence=evidence, source_url=URL)
    assert result["englishComponents"]["state"] == "verified"


def test_unstated_period_is_not_invented_and_other_hosts_unchanged():
    assert facts(page(fee="$18,720"), URL)["fee_term"] is None
    assert not facts(page(), "https://example.edu/course")
    assert not facts("<title>excelsia.edu.au</title>", URL)


def test_domestic_only_has_no_international_fee():
    payload, evidence = {"international_fee": 9100}, []
    apply_authority(page(audience="Domestic students only"), URL, payload, evidence)
    assert payload["international_eligible"] is False
    assert payload["international_fee"] is None


def test_domestic_and_international_amounts_are_not_interchangeable():
    html = page().replace("International students: $8,333 semester/$50,000 in total, Domestic (FEE-HELP)",
                          "Domestic students: $20,800 International students: $18,720")
    assert facts(html, URL)["international_fee"] == 18720


def test_multiline_total_is_not_replaced_by_semester_amount():
    html = page().replace(
        "Tuition Fee: International students: $8,333 semester/$50,000 in total, Domestic (FEE-HELP)",
        "Tuition Fee: International students: $8,333 semester/</p><p>$50,000 in total, Domestic (FEE-HELP)")
    result = facts(html, URL)
    assert result["international_fee"] == 50000
    assert result["fee_term"] == "Full Course"


def test_suffix_audience_and_split_audience_lines():
    html = page().replace(
        "Tuition Fee: International students: $8,333 semester/$50,000 in total, Domestic (FEE-HELP)",
        "Tuition Fee: $11,000 semester/$44,000 in total for International students")
    assert facts(html, URL)["international_fee"] == 44000
    assert facts(html, URL)["fee_term"] == "Full Course"
    html = html.replace(
        "Tuition Fee: $11,000 semester/$44,000 in total for International students",
        "Tuition Fee: Domestic students: $19,040</p><p>International students: $21,680")
    result = facts(html, URL)
    assert result["international_fee"] == 21680 and result["fee_term"] is None


def test_standard_teaching_bands_win_over_conditional_offer():
    html = page().replace("IELTS: 6.0 with no band less than 6.0",
        "IELTS: Overall 7.5 with reading and writing 7.0, speaking and listening at 8.0** "
        "(See Entry Requirement)</p><p>**Applicants with 6.5 overall, no band less than 6.5 may receive a conditional offer.")
    result = facts(html, URL)
    assert result["ielts_overall"] == 7.5
    assert result["ielts_reading"] == result["ielts_writing"] == 7
    assert result["ielts_speaking"] == result["ielts_listening"] == 8
    html = html.replace("reading and writing 7.0, speaking and listening at 8.0",
                        "speaking & listening at 8.0 and reading & writing at 7.0")
    assert facts(html, URL)["ielts_speaking"] == 8
    payload, evidence = {}, []
    apply_authority(html, URL, payload, evidence)
    for item in evidence:
        item["decision_status"] = "selected"
    status = build_requirement_status(SimpleNamespace(**payload), evidence=evidence, source_url=URL)
    assert status["englishComponents"]["state"] == "verified"


def test_delivery_labels_are_not_locations_but_named_campuses_survive():
    from app.services.scraper.extractors.location import _REMOVE_VIRTUAL, _sanitise_for_display
    for value in ("On campus", "on-campus", "In person", "Blended", "Hybrid"):
        assert _REMOVE_VIRTUAL.search(value)
        assert _sanitise_for_display(value) is None
    assert not _REMOVE_VIRTUAL.search("Camperdown Campus")
    assert _sanitise_for_display("Sydney") == "Sydney"


def test_inline_spans_and_introductory_fee_do_not_lose_amount():
    html = page().replace("Tuition Fee: International students: $8,333 semester/$50,000 in total, Domestic (FEE-HELP)",
        "Introductory tuition fee: <span>International students: <strong>$18,460</strong></span>")
    result = facts(html, URL)
    assert result["international_fee"] == 18460
    assert result["fee_term"] is None
