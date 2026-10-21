import asyncio

from app.services.scraper.central_english_tables import level_ielts_cells
from app.services.scraper.central_pages import _parse_english_by_level_async, _parse_program_keyed_english_tables
from app.services.scraper.pipelines.single_course import _select_central_english_program


TABLE = """<table>
<tr><th>Exam/Board</th><th>Undergraduate</th><th>Postgraduate</th><th>Further details</th></tr>
<tr><td>IELTS</td><td>6.0<br>5.5 minimum in each sub-skill</td>
<td>6.5<br>6.0 minimum in each sub-skill</td><td>Some courses require higher scores.</td></tr>
</table>"""


def test_level_columns_are_not_flattened_into_postgraduate_heading():
    profiles = asyncio.run(_parse_english_by_level_async(TABLE, "https://example.edu/english"))
    assert profiles["undergraduate"]["ielts_overall"] == 6.0
    assert profiles["postgraduate"]["ielts_overall"] == 6.5
    for component in ("listening", "reading", "speaking", "writing"):
        assert profiles["undergraduate"][f"ielts_{component}"] == 5.5
        assert profiles["postgraduate"][f"ielts_{component}"] == 6.0


def test_conflicting_rows_are_not_selected_by_document_order():
    html = TABLE + TABLE.replace("6.0<br>", "7.0<br>").replace("6.5<br>", "7.5<br>")
    assert level_ielts_cells(html) == {}
    assert asyncio.run(_parse_english_by_level_async(html, "https://example.edu/english")) == {}


def test_unlabelled_and_ragged_tables_are_not_level_evidence():
    assert level_ielts_cells(TABLE.replace("Postgraduate", "Requirements")) is None
    assert level_ielts_cells(TABLE.replace("<td>Some courses require higher scores.</td>", "")) is None


def test_named_exceptions_keep_their_award_and_component_scope():
    html = """<table><tr><th>Course</th><th>Specific IELTS Requirement</th></tr>
    <tr><td>BA Social Work</td><td>7.0 overall</td></tr>
    <tr><td>MA Social Work</td><td>7.0 overall, 6.5 minimum in each sub-skill</td></tr>
    <tr><td>PGCE Programmes</td><td>6.5 overall, 6.0 in each sub-skill</td></tr></table>"""
    profiles = _parse_program_keyed_english_tables(html)
    assert _select_central_english_program(profiles, "Social Work BA (Hons)") == {"ielts_overall": 7.0}
    masters = _select_central_english_program(profiles, "Social Work MA")
    assert masters["ielts_overall"] == 7.0
    assert masters["ielts_reading"] == 6.5
    pgce = _select_central_english_program(profiles, "PGCE Primary (5-11)")
    assert pgce["ielts_overall"] == 6.5
    assert pgce["ielts_reading"] == 6.0
    assert not _select_central_english_program(profiles, "Primary Education (5-11) with QTS BA (Hons)")


def test_other_english_tests_keep_level_scope_and_native_score_scale():
    html = TABLE.replace("</table>", """
    <tr><td>Pearson</td><td>59 59 in each sub-skill</td><td>64 60 in each sub-skill</td><td></td></tr>
    <tr><td>Cambridge English Examinations</td><td>169 overall</td><td>176 overall</td><td></td></tr>
    <tr><td>TOEFL iBT</td><td>Total Score - 4</td><td>Total Score - 4.5</td><td></td></tr>
    </table>""")
    profiles = asyncio.run(_parse_english_by_level_async(html, "https://example.edu/english"))
    assert profiles["undergraduate"]["pte_overall"] == 59
    assert profiles["postgraduate"]["pte_overall"] == 64
    assert profiles["undergraduate"]["cambridge_overall"] == 169
    assert profiles["postgraduate"]["cambridge_overall"] == 176
    assert "toefl_overall" not in profiles["undergraduate"]
