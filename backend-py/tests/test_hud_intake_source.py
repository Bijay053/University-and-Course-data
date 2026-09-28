"""Huddersfield intake metadata and course-owned content fallback."""
import pytest

from app.services.scraper.config.schema import SearchStaxConfig
from app.services.scraper.searchstax_hud import _hud_intake_source, _map_doc


@pytest.mark.parametrize("metadata", [None, "", "Multiple start dates"])
def test_labelled_dates_replace_unusable_metadata(metadata):
    content = (
        "Select the year2026-272027-28Start Dates"
        "20 September 2027, 10 January 2028, 15 May 2028"
        "Duration18 months with placementApplication deadline 1 August 2027"
    )
    months, raw, method = _hud_intake_source({"start_dates_s": metadata}, content)
    assert months == ["September", "January", "May"]
    assert raw == "20 September 2027, 10 January 2028, 15 May 2028"
    assert method == "searchstax:content:start_dates"


def test_structured_dates_remain_authoritative():
    assert _hud_intake_source(
        {"start_dates_s": "21 September 2026"},
        "Start Dates10 January 2028Duration1 year",
    ) == (["September"], "21 September 2026", "searchstax:start_dates_s")


@pytest.mark.parametrize("content", [
    "Open day 20 September 2027. Apply before 10 January 2028.",
    "Start DatesTo be confirmed. Application deadline 10 January 2028",
    "Start DatesMultiple start datesDuration1 year. Open day 15 May 2028",
    "Start DatesSeptember - DecemberDuration1 year",
])
def test_no_guessing_from_unrelated_dates_or_ranges(content):
    assert _hud_intake_source({}, content)[0] is None


def test_mapper_persists_months_and_exact_source_evidence():
    doc = {
        "url": "https://courses.hud.ac.uk/2027-28/postgraduate/accounting-msc/",
        "h1": ["Accounting and Finance (Professional Practice) MSc"],
        "study_level_s": "Postgraduate",
        "duration_t": "18 months with placement",
        "start_dates_s": "Multiple start dates",
        "content": (
            "Start Dates20 September 2027, 10 January 2028, 15 May 2028"
            "Duration18 months with placement"
        ),
    }
    link = _map_doc(doc, SearchStaxConfig(endpoint="https://example.org/select"))
    result = link["searchstax_result"]
    assert result["payload"]["intake_months"] == ["September", "January", "May"]
    evidence = [e for e in result["evidence"] if e["field_key"] == "intake_months"]
    assert len(evidence) == 1
    assert "20 September 2027" in str(evidence[0])
    assert "searchstax:content:start_dates" in str(evidence[0])