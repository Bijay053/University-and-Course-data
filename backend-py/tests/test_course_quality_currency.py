"""The staged-course quality endpoint must use the persisted fee currency."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.routers.scrape import get_course_quality_scores


def _row(course_id: int, currency: str | None, *, fee_term: str = "Annual",
         duration: float | None = 2, duration_term: str = "year") -> dict:
    return {
        "id": course_id,
        "course_name": "Master of Business",
        "international_fee": 16900,
        "fee_term": fee_term,
        "currency": currency,
        "ielts_overall": 6.5,
        "ielts_reading": None,
        "ielts_writing": None,
        "ielts_listening": None,
        "ielts_speaking": None,
        "pte_overall": None,
        "toefl_overall": None,
        "cambridge_overall": None,
        "duolingo_overall": None,
        "study_mode": "On Campus",
        "degree_level": "Master",
        "course_location": "London",
        "intake_months": ["September"],
        "duration": duration,
        "duration_term": duration_term,
        "course_website": "https://example.ac.uk/courses/master-of-business",
    }


def _scores(rows: list[dict]) -> dict[int, dict]:
    db = SimpleNamespace(execute=AsyncMock(
        return_value=SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows)),
    ))
    result = asyncio.run(get_course_quality_scores(42, db))
    statement, params = db.execute.call_args.args
    assert "currency" in str(statement).split("FROM scraped_courses")[0]
    assert "fee_currency" not in str(statement).split("FROM scraped_courses")[0]
    assert params == {"uni_id": 42}
    return {course["id"]: course for course in result["courses"]}


def _fee_codes(course: dict) -> set[str]:
    return {issue["code"] for issue in course["issues"] if issue["field"] == "fee"}


def test_annual_fee_uses_stored_currency_not_url_country():
    gbp, aud, missing = (_scores([
        _row(1, "GBP"),
        _row(2, "AUD"),
        _row(3, None),
    ])[i] for i in (1, 2, 3))

    assert "annual_fee_too_low_warning" not in _fee_codes(gbp)
    assert gbp["breakdown"]["fee"]["quality"] == "good"
    assert "annual_fee_too_low_warning" in _fee_codes(aud)
    assert aud["breakdown"]["fee"]["quality"] == "medium"
    # Missing currency retains the checker default (AUD), even for a UK URL.
    assert _fee_codes(missing) == _fee_codes(aud)
    assert missing["score"] == aud["score"]
    assert gbp["score"] == aud["score"] + 10


def test_full_course_fee_still_uses_duration_not_annual_fee_range():
    with_duration, without_duration = (_scores([
        _row(4, "GBP", fee_term="Full Course", duration=24, duration_term="months"),
        _row(5, "GBP", fee_term="Full Course", duration=None),
    ])[i] for i in (4, 5))

    assert {"full_course_fee_detected", "full_course_fee_annual_ok"} <= _fee_codes(with_duration)
    assert "8,450/yr" in " ".join(with_duration["breakdown"]["fee"]["issues"])
    assert "full_course_fee_no_duration" not in _fee_codes(with_duration)
    assert {"full_course_fee_detected", "full_course_fee_no_duration"} <= _fee_codes(without_duration)
    assert "full_course_fee_annual_ok" not in _fee_codes(without_duration)
    assert "annual_fee_too_low_warning" not in _fee_codes(with_duration)
    assert "annual_fee_too_low_warning" not in _fee_codes(without_duration)