from types import SimpleNamespace
from pathlib import Path

import pytest
from app.services.scraper.requirement_status import (
    _english_fingerprint,
    build_requirement_status,
    effective_requirement_status,
    is_bounded_qualification_requirement,
    public_requirement_status,
)
from app.services.scraper.completeness import compute_completeness
from app.services.scraper.extractors import english_test
from app.services.scraper.extractors.londonmet_academic import apply_fill_only
from app.services.scraper.central_pages import _parse_english_page_html_async


def _row(**overrides):
    values = {
        "academic_score": None,
        "score_type": None,
        "academic_level": "Postgraduate",
        "other_requirement": None,
        "ielts_overall": None,
        "ielts_listening": None,
        "ielts_speaking": None,
        "ielts_writing": None,
        "ielts_reading": None,
        "requirement_status": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _evidence(
    field,
    snippet,
    source_url="https://www.londonmet.ac.uk/courses/example",
    method="heuristic_heading",
):
    return {
        "field_key": field,
        "snippet": snippet,
        "source_url": source_url,
        "method": method,
        "decision_status": "selected",
    }


def test_spurious_qualification_prose_is_not_proof():
    prose = "This course helps applicants develop professional qualifications and credit."
    row = _row(other_requirement=prose)
    status = build_requirement_status(
        row,
        evidence=[_evidence("other_requirement", prose)],
    )
    assert not is_bounded_qualification_requirement(prose)
    assert status["academic"]["state"] == "unverified"
    assert row.academic_score is None


def test_official_classification_is_qualification_based_without_inventing_score():
    text = "Applicants must hold a minimum lower second-class honours degree (2:2)."
    row = _row(other_requirement=text)
    status = build_requirement_status(
        row,
        evidence=[_evidence("other_requirement", text)],
        source_url="https://www.londonmet.ac.uk/courses/example",
    )
    assert status["academic"]["state"] == "qualification_based"
    assert status["academic"]["sourceUrl"].startswith("https://")
    assert row.academic_score is None


def test_londonmet_entry_panel_evidence_is_trusted_qualification_proof():
    html = (Path(__file__).parent / "fixtures" / "londonmet_mba_entry.html").read_text(
        encoding="utf-8"
    )
    url = "https://www.londonmet.ac.uk/courses/postgraduate/example/"
    payload: dict = {}
    evidence: list[dict] = []
    apply_fill_only(payload, html, url=url, evidence=evidence)
    row = _row(
        academic_level=payload.get("academic_level"),
        other_requirement=payload.get("other_requirement"),
    )
    assert evidence[0]["method"] == "londonmet_academic:entry_panel"
    assert build_requirement_status(
        row, evidence=evidence, source_url=url
    )["academic"]["state"] == "qualification_based"


def test_missing_or_invalid_source_cannot_verify_qualification():
    text = "Applicants must hold a minimum lower second-class honours degree (2:2)."
    row = _row(other_requirement=text)
    for source in (None, "not-a-url", "javascript:alert(1)"):
        evidence = [{
            "field_key": "other_requirement",
            "snippet": text,
            "source_url": source,
            "method": "heuristic_heading",
        }]
        assert build_requirement_status(row, evidence=evidence)["academic"]["state"] == "unverified"


def test_cross_site_citation_does_not_prove_official_qualification():
    text = "Applicants must hold a minimum lower second-class honours degree (2:2)."
    row = _row(other_requirement=text)
    status = build_requirement_status(
        row,
        evidence=[_evidence("other_requirement", text, "https://example.net/requirements")],
        source_url="https://www.londonmet.ac.uk/courses/example",
    )
    assert status["academic"]["state"] == "unverified"


def test_conflicting_classification_citation_is_not_proof():
    row_text = "Applicants must hold a minimum lower second-class honours degree (2:2)."
    evidence_text = "Applicants must hold an upper second-class honours degree (2:1)."
    row = _row(other_requirement=row_text)
    status = build_requirement_status(
        row,
        evidence=[_evidence("other_requirement", evidence_text)],
        source_url="https://www.londonmet.ac.uk/courses/example",
    )
    assert status["academic"]["state"] == "unverified"


def test_persisted_proof_survives_reload_but_manual_edit_invalidates_it():
    text = "Applicants must hold a minimum lower second-class honours degree (2:2)."
    row = _row(other_requirement=text)
    row.requirement_status = build_requirement_status(
        row,
        evidence=[_evidence("other_requirement", text)],
        source_url="https://www.londonmet.ac.uk/courses/example",
    )
    assert effective_requirement_status(row)["academic"]["state"] == "qualification_based"

    row.other_requirement = "Applicants should review the general entry requirements."
    assert effective_requirement_status(row)["academic"]["state"] == "unverified"


def test_old_row_never_upgrades_prose_without_persisted_proof():
    text = "Applicants must hold a minimum lower second-class honours degree (2:2)."
    row = _row(other_requirement=text, requirement_status=None)
    assert public_requirement_status(row)["academic"]["state"] == "unverified"


def test_ielts_overall_reports_every_missing_component():
    row = _row(ielts_overall=6.5, ielts_writing=6.0)
    english = build_requirement_status(row)["englishComponents"]
    assert english == {
        "state": "missing",
        "missingFields": [
            "ielts_listening",
            "ielts_speaking",
            "ielts_reading",
        ],
    }


def test_complete_ielts_values_are_unknown_until_source_verified():
    row = _row(
        ielts_overall=6.5,
        ielts_listening=6.0,
        ielts_speaking=6.0,
        ielts_writing=6.0,
        ielts_reading=6.0,
    )
    assert build_requirement_status(row)["englishComponents"]["state"] == "unknown"

    evidence = [
        _evidence(field, "Minimum 6.0 in each IELTS component")
        for field in (
            "ielts_listening",
            "ielts_speaking",
            "ielts_writing",
            "ielts_reading",
        )
    ]
    persisted = build_requirement_status(row, evidence=evidence)
    assert persisted["englishComponents"]["state"] == "verified"
    row.requirement_status = persisted
    assert public_requirement_status(row)["englishComponents"]["state"] == "verified"


def test_ielts_score_normalizes_six_and_six_point_zero():
    row = _row(
        ielts_overall=6.5,
        ielts_listening=6,
        ielts_speaking=6,
        ielts_writing=6,
        ielts_reading=6,
    )
    evidence = [
        _evidence(field, "Minimum 6.0 in each IELTS component")
        for field in (
            "ielts_listening",
            "ielts_speaking",
            "ielts_writing",
            "ielts_reading",
        )
    ]
    assert build_requirement_status(row, evidence=evidence)["englishComponents"]["state"] == "verified"


def test_ielts_overall_number_cannot_cross_match_lower_band_floor():
    row = _row(
        ielts_overall=6.0,
        ielts_listening=6.0,
        ielts_speaking=6.0,
        ielts_writing=6.0,
        ielts_reading=6.0,
    )
    snippet = "IELTS 6.0 overall with a minimum 5.5 in each component."
    evidence = [
        _evidence(field, snippet)
        for field in (
            "ielts_listening",
            "ielts_speaking",
            "ielts_writing",
            "ielts_reading",
        )
    ]
    assert build_requirement_status(row, evidence=evidence)["englishComponents"]["state"] == "unknown"


def test_correct_lower_all_band_floor_is_semantically_bound():
    row = _row(
        ielts_overall=6.0,
        ielts_listening=5.5,
        ielts_speaking=5.5,
        ielts_writing=5.5,
        ielts_reading=5.5,
    )
    snippet = "IELTS 6.0 overall with a minimum 5.5 in each component."
    evidence = [
        _evidence(field, snippet)
        for field in (
            "ielts_listening",
            "ielts_speaking",
            "ielts_writing",
            "ielts_reading",
        )
    ]
    assert build_requirement_status(row, evidence=evidence)["englishComponents"]["state"] == "verified"


def test_ai_only_component_evidence_is_not_verification():
    row = _row(
        ielts_overall=6.5,
        ielts_listening=6.0,
        ielts_speaking=6.0,
        ielts_writing=6.0,
        ielts_reading=6.0,
    )
    evidence = [
        _evidence(
            field,
            "Minimum 6.0 in each IELTS component",
            method="openai_primary",
        )
        for field in (
            "ielts_listening",
            "ielts_speaking",
            "ielts_writing",
            "ielts_reading",
        )
    ]
    assert build_requirement_status(row, evidence=evidence)["englishComponents"]["state"] == "unknown"


def test_unsupported_not_required_claim_is_not_preserved_even_with_matching_hash():
    row = _row()
    source_url = "https://www.londonmet.ac.uk/english-requirements"
    row.requirement_status = {
        "academic": {"state": "missing"},
        "englishComponents": {
            "state": "not_required",
            "sourceUrl": source_url,
            "_fingerprint": _english_fingerprint(row, source_url=source_url),
        },
    }
    assert effective_requirement_status(row)["englishComponents"]["state"] == "unknown"


@pytest.mark.asyncio
async def test_english_test_grouped_evidence_verifies_all_component_bands():
    url = "https://www.example.edu.au/english-requirements"
    results = await english_test.extract(
        "<p>Academic IELTS overall 6.5 with no band below 6.0.</p>", url
    )
    grouped = next(result for result in results if result.field_key == "ielts_overall")
    row = _row(**grouped.normalized)
    evidence = [{
        "field_key": grouped.field_key,
        "value": grouped.value,
        "snippet": grouped.snippet,
        "source_url": url,
        "method": grouped.method,
    }]
    assert build_requirement_status(row, evidence=evidence, source_url=url)[
        "englishComponents"
    ]["state"] == "verified"


@pytest.mark.asyncio
async def test_central_english_parser_preserves_real_grouped_proof_for_staging():
    url = "https://www.example.edu.au/english-requirements"
    values = await _parse_english_page_html_async(
        "<p>Academic IELTS overall 6.5 with no band below 6.0.</p>", url
    )
    proof = values["_requirement_evidence"]
    row = _row(**{
        key: value for key, value in values.items() if key.startswith("ielts_")
    })
    evidence = [{
        "field_key": "ielts_overall",
        "snippet": proof["snippet"],
        "source_url": url,
        "method": proof["method"],
    }]
    assert build_requirement_status(row, evidence=evidence, source_url=url)[
        "englishComponents"
    ]["state"] == "verified"


def test_rebuilding_unchanged_persisted_status_is_stable_for_truthful_progress():
    text = "Applicants must hold a minimum lower second-class honours degree (2:2)."
    row = _row(other_requirement=text)
    first = build_requirement_status(
        row,
        evidence=[_evidence("other_requirement", text)],
        source_url="https://www.londonmet.ac.uk/courses/example",
    )
    row.requirement_status = first
    second = build_requirement_status(row, previous=first)
    assert second == first


def test_qualification_based_requirement_satisfies_score_slot_without_a_number():
    text = "Applicants must hold a minimum lower second-class honours degree (2:2)."
    row = _row(
        scrape_job_id="job",
        university_id=68,
        course_name="Master of Example",
        degree_level="Master's",
        category="Business",
        study_mode="On Campus",
        course_location="London",
        duration=1,
        intake_months=["September"],
        international_fee=18000,
        description="Course description",
        pte_overall=60,
        other_requirement=text,
    )
    row.requirement_status = build_requirement_status(
        row,
        evidence=[_evidence("other_requirement", text)],
        source_url="https://www.londonmet.ac.uk/courses/example",
    )
    result = compute_completeness(row)
    assert "academicScore" in result.filled
    assert row.academic_score is None