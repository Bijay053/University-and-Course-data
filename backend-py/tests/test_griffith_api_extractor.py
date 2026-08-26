import pytest

from app.services.scraper.extractors.griffith_api import (
    is_griffith_degree_url,
    map_program,
    program_document_html,
    program_code_from_url,
    search_result_to_program,
)


def _program():
    return {
        "code": "1703",
        "title": "Bachelor of Communication/Bachelor of Business",
        "campuses": [
            {"name": "Brisbane South", "code": "NA"},
            {"name": "Gold Coast", "code": "GC"},
        ],
        "duration": [
            {"studentType": "Domestic", "fullTime": "4", "partTime": "8"},
            {"studentType": "International", "fullTime": "4", "partTime": None},
        ],
        "knownFees": [
            {
                "year": 2026,
                "fees": [
                    {
                        "amount": 17400,
                        "band": {"feeType": {"code": "COMM"}},
                    },
                    {
                        "amount": 37000,
                        "band": {"feeType": {"code": "INTL"}},
                    },
                ],
            }
        ],
        "intakes": [
            {"name": "Trimester 1", "isInternational": True},
            {"name": "Trimester 2", "isInternational": True},
        ],
        "admissionDetails": [
            {"englishLanguageScore": 6.5},
            {"englishLanguageScore": 6.5},
        ],
        "cricos": "116977A",
        "academicCareer": {"name": "Undergraduate", "code": "UGRD"},
        "internationalEntryRequirement": {
            "pathway": "<p>Post-secondary qualifications are required.</p>"
        },
        "programOverviews": [
            {
                "studentType": "International",
                "overview": "<p>Build communication and business expertise.</p>",
            }
        ],
    }


def test_maps_visible_international_key_facts():
    assert map_program(_program()) == {
        "course_name": "Bachelors of Communication / Business",
        "course_location": "Brisbane South, Gold Coast",
        "study_mode": "On Campus",
        "duration": 4.0,
        "duration_term": "Year",
        "study_load": "Full Time",
        "international_fee": 37000.0,
        "fee_term": "Annual",
        "fee_year": 2026,
        "currency": "AUD",
        "intake_months": ["March", "July"],
        "ielts_overall": 6.5,
        "cricos_code": "116977A",
        "degree_level": "Undergraduate",
        "description": "Build communication and business expertise.",
        "other_requirement": "Post-secondary qualifications are required.",
    }


def test_builds_substantive_html_without_spa_fetch():
    html = program_document_html(_program())
    assert "Bachelors of Communication / Business" in html
    assert "37000.0" in html
    assert "116977A" in html
    assert len(html) > 500


def test_maps_retired_program_from_funnelback_metadata():
    result = {
        "title": "Master of International Business/Master of International Relations",
        "liveUrl": "https://www.griffith.edu.au/study/degrees/example-5659",
        "listMetadata": {
            "title": [
                "Master of International Business/Master of International Relations"
            ],
            "campusName": ["Online", "Brisbane South"],
            "duration": ["Domestic", "4", "2", "International", "4 (online only)", "2"],
            "intlFee": ["41000"],
            "knownFeesYear": ["2026", "2026"],
            "intakesName": ["Trimester 1", "Trimester 2"],
            "intakesIsInternational": ["true", "true"],
            "academicCareer": ["Postgraduate"],
            "academicCareerCode": ["PGRD"],
            "descriptionStudentType": ["Domestic", "International"],
            "description": ["Domestic overview", "<p>International overview.</p>"],
            "requirementsSummary": ["<p>Related Bachelor degree or higher</p>"],
        },
    }
    mapped = map_program(search_result_to_program(result, "5659"))
    assert mapped["course_name"] == (
        "Masters of International Business / International Relations"
    )
    assert mapped["course_location"] == "Brisbane South"
    assert mapped["study_mode"] == "Mixed"
    assert mapped["duration"] == 2.0
    assert mapped["international_fee"] == 41000.0
    assert mapped["intake_months"] == ["March", "July"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://www.griffith.edu.au/study/degrees/"
            "bachelor-of-communication-bachelor-of-business-1703?location=intl",
            "1703",
        ),
        ("https://www.griffith.edu.au/study/degrees?term=", None),
        ("https://www.griffith.edu.au/study/courses/1001", None),
    ],
)
def test_program_code_is_only_read_from_degree_detail_urls(url, expected):
    assert program_code_from_url(url) == expected
    assert is_griffith_degree_url(url) is (expected is not None)