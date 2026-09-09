from __future__ import annotations

from pathlib import Path

from app.services.scraper.central_pages import (
    _parse_column_keyed_english_table,
    _parse_program_keyed_english_tables,
)
from app.services.scraper.extractors import duration
from app.services.scraper.pipelines.single_course import (
    _is_structural_course_page_method,
    _select_central_english_program,
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


def test_murdoch_higher_education_profile_matches_exact_course_codes() -> None:
    html = """
    <div class="accordion-item">
      <div class="accordion-body">
        <p><strong>Bachelor of Education</strong> (B1368, B1404, B1405, B1406)</p>
        <p>These requirements apply to all undergraduate Education courses.</p>
        <table>
          <tr>
            <th>International English Language Testing System (IELTS) Academic</th>
            <td><strong>7.5 Overall</strong> 7.0 Reading 7.0 Writing 8.0 Speaking 8.0 Listening</td>
          </tr>
          <tr><th>Cambridge Advanced English (CAE)</th><td><strong>191 Overall</strong></td></tr>
          <tr><th>Pearson Test of English (PTE)</th><td><strong>76 Overall</strong></td></tr>
          <tr><th>TOEFL IBT</th><td><strong>102 Overall</strong></td></tr>
          <tr><th>Duolingo English Test (DET)</th><td>N/A</td></tr>
        </table>
      </div>
    </div>
    """

    profiles = _parse_program_keyed_english_tables(html)
    expected = {
        "ielts_overall": 7.5,
        "ielts_reading": 7.0,
        "ielts_writing": 7.0,
        "ielts_speaking": 8.0,
        "ielts_listening": 8.0,
        "cambridge_overall": 191.0,
        "pte_overall": 76.0,
        "toefl_overall": 102.0,
    }

    for code in ("b1368", "b1404", "b1405", "b1406"):
        assert _select_central_english_program(
            profiles,
            "Bachelor of Education (Primary, 1-10 Health and Physical Education)",
            f"https://www.murdoch.edu.au/course/undergraduate/{code}",
        ) == expected

    assert _select_central_english_program(
        profiles,
        "Master of Education",
        "https://www.murdoch.edu.au/course/postgraduate/m1367",
    ) == {}


def test_murdoch_profile_ignores_blank_strong_before_b1422_table() -> None:
    html = """
    <div class="accordion-item">
      <div class="accordion-body">
        <p><strong>Bachelor of Health Science / Master of Clinical Chiropractic</strong>
           (B1422)</p>
        <p><strong>&nbsp;</strong></p>
        <table>
          <tr>
            <th>International English Language Testing System (IELTS) Academic</th>
            <td><strong>6.5 Overall</strong> 6.5 Reading 6.5 Writing
                6.5 Speaking 6.5 Listening</td>
          </tr>
          <tr><th>Cambridge Advanced English (CAE)</th><td><strong>176 Overall</strong></td></tr>
          <tr><th>Pearson Test of English (PTE)</th><td><strong>58 Overall</strong></td></tr>
          <tr><th>TOEFL IBT</th><td><strong>79 Overall</strong></td></tr>
          <tr><th>Duolingo English Test (DET)</th><td><strong>120 Overall</strong></td></tr>
        </table>
      </div>
    </div>
    """

    profiles = _parse_program_keyed_english_tables(html)

    assert _select_central_english_program(
        profiles,
        "Bachelor of Health Science / Master of Clinical Chiropractic",
        "https://www.murdoch.edu.au/course/undergraduate/b1422",
    ) == {
        "ielts_overall": 6.5,
        "ielts_reading": 6.5,
        "ielts_writing": 6.5,
        "ielts_speaking": 6.5,
        "ielts_listening": 6.5,
        "cambridge_overall": 176.0,
        "pte_overall": 58.0,
        "toefl_overall": 79.0,
        "duolingo_overall": 120.0,
    }


def test_murdoch_category_accordion_parses_each_course_table() -> None:
    html = """
    <div class="accordion-item">
      <div class="accordion-body">
        <p><strong>First Allied Health Course</strong> (B1001)</p>
        <table>
          <tr><th>IELTS Academic</th><td>6.5 Overall</td></tr>
        </table>
        <p><strong>Second Allied Health Course</strong> (M1002)</p>
        <table>
          <tr><th>IELTS Academic</th><td>7.0 Overall</td></tr>
        </table>
      </div>
    </div>
    """

    profiles = _parse_program_keyed_english_tables(html)

    assert [profile["course_codes"] for profile in profiles] == [
        ["B1001"],
        ["M1002"],
    ]
    assert [profile["values"]["ielts_overall"] for profile in profiles] == [
        6.5,
        7.0,
    ]


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