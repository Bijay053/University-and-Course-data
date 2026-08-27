from __future__ import annotations

from pathlib import Path

from app.services.scraper.central_pages import _parse_column_keyed_english_table
from app.services.scraper.extractors import duration
from app.services.scraper.pipelines.single_course import (
    _is_structural_course_page_method,
)


def test_murdoch_config_uses_live_english_proficiency_page() -> None:
    config = (
        Path(__file__).parents[1] / "scraper_config" / "unis" / "murdoch.yaml"
    ).read_text()
    assert (
        "https://www.murdoch.edu.au/study/how-to-apply/entry-requirements/"
        "english-proficiency-tests"
    ) in config
    assert "study/international-students/entry-requirements" not in config


def test_value_first_level_table_extracts_all_supported_english_tests() -> None:
    html = """
    <table>
      <tr>
        <th>Proficiency test</th>
        <th>Undergraduate minimum requirements</th>
        <th>Postgraduate minimum requirements</th>
        <th>Research degree minimum requirements</th>
      </tr>
      <tr>
        <td>IELTS Academic</td>
        <td>6.0 Overall 6.0 Reading</td>
        <td>6.0 Overall 6.0 Reading</td>
        <td>6.5 Overall 6.0 Reading</td>
      </tr>
      <tr>
        <td>Pearson Test of English (PTE)</td>
        <td>50 Overall 53 Reading</td>
        <td>50 Overall 53 Reading</td>
        <td>58 Overall 53 Reading</td>
      </tr>
      <tr>
        <td>TOEFL IBT</td>
        <td>60 Overall 13 Reading</td>
        <td>60 Overall 13 Reading</td>
        <td>79 Overall 13 Reading</td>
      </tr>
      <tr>
        <td>Cambridge Advanced English</td>
        <td>169 Overall 169 Reading</td>
        <td>169 Overall 169 Reading</td>
        <td>176 Overall 169 Reading</td>
      </tr>
      <tr>
        <td>Duolingo English Test</td>
        <td>115 Overall 110 Reading</td>
        <td>115 Overall 110 Reading</td>
        <td>120 Overall 110 Reading</td>
      </tr>
    </table>
    """

    flat, by_level = _parse_column_keyed_english_table(html)

    assert flat == {
        "ielts_overall": 6.5,
        "pte_overall": 58.0,
        "toefl_overall": 79.0,
        "cambridge_overall": 176.0,
        "duolingo_overall": 120.0,
    }
    assert by_level["undergraduate"] == {
        "ielts_overall": 6.0,
        "pte_overall": 50.0,
        "toefl_overall": 60.0,
        "cambridge_overall": 169.0,
        "duolingo_overall": 115.0,
    }
    assert by_level["postgraduate"] == by_level["undergraduate"]
    assert by_level["doctorate"] == flat


def test_bare_numeric_full_time_duration_uses_label_semantics() -> None:
    html = """
    <dl>
      <div>
        <dt>Full time duration</dt>
        <dd>4</dd>
      </div>
      <p>Applicants may qualify after completing 2 years of prior study.</p>
    </dl>
    """

    import asyncio

    out = asyncio.run(duration.extract(html, "https://example.edu/course/b1362"))

    assert out
    assert out[0].normalized == {
        "duration": 4.0,
        "duration_term": "Year",
    }
    assert out[0].method == "duration.structural"


def test_audience_structural_fee_is_protected_from_ai_overwrite() -> None:
    assert _is_structural_course_page_method("fee.audience_structural")