from __future__ import annotations

from app.services.scraper.central_pages import _parse_column_keyed_english_table
from app.services.scraper.pipelines.single_course import (
    _select_central_english_level,
)


def test_unisc_standard_requirements_table_is_level_aware() -> None:
    html = """
    <table>
      <tr>
        <th>Test or other preparation</th>
        <th>Undergraduate (including undergraduate Study Abroad and TPP)</th>
        <th>Postgraduate coursework (including PQP and Honours)</th>
        <th>Higher Degrees by Research (including Doctoral degrees)</th>
      </tr>
      <tr>
        <td>IELTS (Academic)<br>IELTS One Skill Retake accepted</td>
        <td>Overall score of 6.0 with minimum 5.5 in each subtest</td>
        <td>Overall score of 6.5 with minimum 6.0 in each subtest</td>
        <td>Overall score of 6.5 with minimum 6.0 in each subtest</td>
      </tr>
      <tr>
        <td>TOEFL iBT</td>
        <td>Overall score of 76 with minimum score of 18 for writing</td>
        <td>Overall score of 85 with minimum score of 21 for writing</td>
        <td>Overall score of 85 with minimum score of 21 for writing</td>
      </tr>
      <tr>
        <td>Pearson Test of English (PTE)</td>
        <td>Overall score of 50 with no subscore less than 50</td>
        <td>Overall score of 58 with no subscore less than 54</td>
        <td>Overall score of 58 with no subscore less than 54</td>
      </tr>
    </table>
    """

    flat, by_level = _parse_column_keyed_english_table(html)

    assert by_level["undergraduate"] == {
        "ielts_overall": 6.0,
        "toefl_overall": 76.0,
        "pte_overall": 50.0,
    }
    assert by_level["postgraduate"] == {
        "ielts_overall": 6.5,
        "toefl_overall": 85.0,
        "pte_overall": 58.0,
    }
    assert by_level["doctorate"] == by_level["postgraduate"]
    assert flat == by_level["doctorate"]


def test_doctorate_uses_research_requirement_with_postgraduate_fallback() -> None:
    by_level = {
        "undergraduate": {"ielts_overall": 6.0},
        "postgraduate": {"ielts_overall": 6.5},
        "doctorate": {"ielts_overall": 7.0},
    }
    bucket, values = _select_central_english_level(by_level, "Doctorate")
    assert bucket == "doctorate"
    assert values == {"ielts_overall": 7.0}

    bucket, values = _select_central_english_level(
        {"postgraduate": {"ielts_overall": 6.5}},
        "Doctorate",
    )
    assert bucket == "doctorate"
    assert values == {"ielts_overall": 6.5}