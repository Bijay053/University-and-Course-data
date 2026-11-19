from unittest.mock import AsyncMock
import pytest

from app.services.scraper.extractors.tru_open_learning_fees import TUITION, parse, extract
from app.services.scraper.extractors import tru_calculator

URL = "https://www.tru.ca/distance/programs/advanced-diploma-management.html"
PROGRAMME = f"""<main><h1>Advanced Diploma in Management</h1>
<script>var catalogue=[{{"name":"An unrelated award","description":
"This program is not currently accepting new applications"}}];</script>
<a href="{TUITION}">Tuition and Fees</a></main>"""
SCHEDULE = """<main><h1>Open Learning Tuition</h1><table>
<tr><th>Undergraduate tuition</th><td>Rates</td></tr>
<tr><td>Canadian citizens</td><td>Variable (average $183.04 per credit)</td></tr>
<tr><td>International students</td><td>$514.66 per credit</td></tr>
</table><p>All fees are payable in
Canadian funds.</p></main>"""


def test_owned_open_learning_qualification_uses_international_credit_rate_not_four_course_math():
    value = parse(PROGRAMME, SCHEDULE, URL)
    assert value.value == 514.66
    assert value.normalized == {"international_fee":514.66,"currency":"CAD","fee_term":"Per Credit","fee_year":None}
    assert value.extras["tuition_source_url"] == TUITION
    assert value.extras["course_url"] == URL
    assert value.snippet == "International students $514.66 per credit"


@pytest.mark.parametrize("title", ["Master of Education", "Graduate Certificate in Education", "Doctor of Philosophy",
                                  "Post-Baccalaureate Diploma in Education"])
def test_undergraduate_profile_never_applies_to_graduate_awards(title):
    assert parse(PROGRAMME.replace("Advanced Diploma in Management", title), SCHEDULE, URL) is None


@pytest.mark.parametrize("schedule", [
    SCHEDULE.replace("International students", "Canadian citizens and permanent residents"),
    SCHEDULE.replace("$514.66 per credit", "Variable (average $514.66 per credit)"),
    SCHEDULE.replace("Undergraduate tuition", "Graduate tuition"),
    SCHEDULE.replace("Canadian funds", "unspecified funds"),
    SCHEDULE.replace("</table>", "<tr><td>International students</td><td>$600.00 per credit</td></tr></table>"),
])
def test_missing_conflicting_or_inapplicable_rate_never_becomes_tuition(schedule):
    assert parse(PROGRAMME, schedule, URL) is None


def test_foreign_campus_closed_and_custom_tuition_are_not_given_the_generic_rate():
    assert parse(PROGRAMME, SCHEDULE, "https://www.tru.ca/programs/catalogue/example.html") is None
    assert parse(PROGRAMME, SCHEDULE, URL.replace("www.tru.ca","example.edu")) is None
    assert parse(PROGRAMME.replace("</main>","<p>This program is not currently accepting new applications</p></main>"), SCHEDULE, URL) is None
    assert parse(PROGRAMME.replace("</main>","<p>Tuition for this programme is CAD 900 per credit.</p></main>"), SCHEDULE, URL) is None
    assert parse(PROGRAMME.replace(TUITION, "https://example.edu/tuition.html"), SCHEDULE, URL) is None


@pytest.mark.asyncio
async def test_failed_source_access_propagates_without_rate_or_bypass(monkeypatch):
    read = AsyncMock(side_effect=ValueError("robots disallows source"))
    monkeypatch.setattr(tru_calculator, "read_dependency", read)
    with pytest.raises(ValueError, match="robots"):
        await extract(PROGRAMME, URL)
    read.assert_awaited_once()
