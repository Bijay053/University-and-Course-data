"""Tests for the newly ported extractors: location, eligibility,
course_name, and the provenance footer helper."""
from __future__ import annotations

import pytest

from app.services.scraper.extractors import course_name, eligibility, location
from app.services.scraper.provenance import build_course_page_provenance_footer


# ─── location ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_location_from_definition_list() -> None:
    html = """
    <html><body>
      <dl><dt>Campus</dt><dd>Sydney, Melbourne</dd></dl>
    </body></html>
    """
    out = await location.extract(html, "https://x.test")
    assert out and out[0].value == "Sydney, Melbourne"
    assert out[0].method == "location.dl"


@pytest.mark.asyncio
async def test_location_strips_online_virtual() -> None:
    html = '<dl><dt>Location</dt><dd>Online, Brisbane, Virtual</dd></dl>'
    out = await location.extract(html, "")
    assert out[0].value == "Brisbane"


@pytest.mark.asyncio
async def test_location_rejects_marketing_paragraph() -> None:
    long_marketing = (
        "<p>Locations</p><p>This program focuses on delivering knowledge and "
        "skills in computer science across many emerging tech areas.</p>"
    )
    out = await location.extract(long_marketing, "")
    # Marketing copy must NOT be returned as a location.
    assert out == [] or out[0].value not in ("This program focuses",)


@pytest.mark.asyncio
async def test_location_text_block_picks_known_city() -> None:
    html = (
        "<body>Campus locations: Our flagship campus is in Wollongong "
        "with classes also offered in Cairns and Townsville. Intakes February.</body>"
    )
    out = await location.extract(html, "")
    assert out, "should detect a city from free-text block"
    val = out[0].value
    assert "Wollongong" in val
    assert "Cairns" in val
    assert "Townsville" in val


# ─── eligibility ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_eligibility_positive_cricos() -> None:
    html = "<p>CRICOS code: 091234A</p>"
    out = await eligibility.extract(html, "")
    assert out and out[0].value is True


@pytest.mark.asyncio
async def test_eligibility_negative_domestic_only() -> None:
    html = "<p>This course is open to domestic students only.</p>"
    out = await eligibility.extract(html, "")
    assert out and out[0].value is False
    assert out[0].normalized["eligibility_status"] == "rejected"


@pytest.mark.asyncio
async def test_eligibility_unknown_returns_empty() -> None:
    html = "<p>Welcome to our university.</p>"
    out = await eligibility.extract(html, "")
    assert out == []


# ─── course_name ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_course_name_from_h1_smart_case() -> None:
    html = "<h1>BACHELOR OF BUSINESS</h1>"
    out = await course_name.extract(html, "")
    assert out and out[0].value == "Bachelor of Business"


@pytest.mark.asyncio
async def test_course_name_strips_university_suffix() -> None:
    html = "<h1>Master of IT | Charles Sturt University</h1>"
    out = await course_name.extract(html, "")
    assert out[0].value == "Master of IT"


@pytest.mark.asyncio
async def test_course_name_falls_back_to_title() -> None:
    html = "<title>MBA - USQ</title><body><p>no h1</p></body>"
    out = await course_name.extract(html, "")
    assert out and out[0].value == "MBA"
    assert out[0].method == "course_name.title"


# ─── provenance footer ──────────────────────────────────────────────────


def test_provenance_footer_empty_when_no_fields() -> None:
    assert build_course_page_provenance_footer({}) == ""


def test_provenance_footer_formats_known_fields() -> None:
    footer = build_course_page_provenance_footer(
        {
            "course_name": "Bachelor of IT",
            "degree_level": "Bachelor",
            "duration": 3,
            "duration_term": "Year",
            "course_location": "Sydney",
            "international_fee": 35000,
            "currency": "AUD",
            "fee_term": "per year",
            "intake_months": ["February", "July"],
            "ielts_overall": 6.5,
        }
    )
    assert footer.startswith("\n\n[course-page extracted fields] ")
    assert "courseName: Bachelor of IT" in footer
    assert "international fee: AUD 35000 per year" in footer
    assert "intake: February, July" in footer
    assert "IELTS 6.5" in footer
    assert footer.endswith(".")
