import pytest

from app.services.scraper.extractors.selected_intake import extract_selected_intake

BASE = "https://www.aston.ac.uk/study/courses/digital-chemical-engineering-msc"
HTML = f"""<html><head><link rel="canonical" href="{BASE}"></head><body>
<main><h1>Digital Chemical Engineering MSc</h1><div class="cr_start_date">
<p class="ques"> Start date</p><div class="info select-wrapper">
<select class="other_variant_switch default" onchange="window.location=''+this.value;">
<option value="{BASE}/january-2027">January 2027</option>
<option selected="selected" value="{BASE}">September 2027</option>
</select></div></div></main></body></html>"""


def test_official_selected_canonical_start_owns_its_dated_alias():
    result = extract_selected_intake(HTML, BASE + "/september-2027")
    assert len(result) == 1
    assert result[0].value == ["September"]
    assert result[0].method == "intake:selected_start_option"
    assert "September 2027" in result[0].snippet


@pytest.mark.parametrize("change", [
    "no_canonical", "foreign_canonical", "different_course", "conflicting_canonicals",
    "different_year", "different_month", "query", "unselected", "duplicate_selected",
])
def test_alias_cannot_borrow_an_unowned_or_ambiguous_selected_date(change):
    html, url = HTML, BASE + "/september-2027"
    if change == "no_canonical":
        html = html.replace(f'<link rel="canonical" href="{BASE}">', "")
    elif change == "foreign_canonical":
        html = html.replace(f'href="{BASE}"', 'href="https://unrelated.example/course"')
    elif change == "different_course":
        html = html.replace(f'value="{BASE}"', f'value="{BASE}-conversion"')
    elif change == "conflicting_canonicals":
        html = html.replace("</head>", '<link rel="canonical" href="https://unrelated.example/course"></head>')
    elif change == "different_year":
        url = BASE + "/september-2026"
    elif change == "different_month":
        url = BASE + "/april-2027"
    elif change == "query":
        url += "?campus=london"
    elif change == "unselected":
        html = html.replace(' selected="selected"', "")
    else:
        html = html.replace(f'value="{BASE}/january-2027"', f'selected value="{BASE}/january-2027"')
    assert extract_selected_intake(html, url) == []


@pytest.mark.asyncio
async def test_full_extractor_and_chat_evidence_choose_september_not_first_january():
    from app.services.scrape_assistant_tools import inspect_source
    from app.services.assistant_intake_evidence import intake_proof
    from app.services.scraper.extractors.intake import extract
    # Enough substantive course content for the ordinary source validation gate.
    html = HTML.replace("</main>", "<p>Duration One year. Entry requirements UK 2:2 honours degree. "
                        "Course overview and modules. International tuition fee £24,800.</p></main>")
    url = BASE + "/september-2027"
    extracted = await extract(html, url)
    assert extracted[0].normalized["intake_months"] == ["September"]
    source = await inspect_source(url, "https://www.aston.ac.uk", raw_body=html.encode())
    assert source["verified"] is True
    from app.services.scraper.extractors.selected_intake import INTAKE_FACTS_VERSION
    assert source["_intakeFactsVersion"] == INTAKE_FACTS_VERSION
    proof = intake_proof(source, url, "Digital Chemical Engineering MSc (September 2027)")
    assert proof and proof[0] == ["September"]


@pytest.mark.asyncio
async def test_old_january_cache_is_refreshed_once_not_reused_after_alias_fix(monkeypatch):
    from copy import deepcopy
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.services import assistant_source_cache as cache, assistant_retained_source
    from app.services.scraper.extractors.selected_intake import INTAKE_FACTS_VERSION
    from tests.test_assistant_access_failures import Store
    store = Store()
    url = BASE + "/september-2027"
    old = {"url": url, "verified": True, "sourceType": "html", "text": "Start date January 2027",
           "_sourceScopeVersion": 2, "_intakeFactsVersion": 1}
    store.thread.messages = [{"role": "user", "_sourceChecks": {url: deepcopy(old)}}]
    monkeypatch.setattr(assistant_retained_source, "retained_course_source", AsyncMock(return_value=None))
    fresh = {**old, "_intakeFactsVersion": INTAKE_FACTS_VERSION, "text": "Start date September 2027",
             "_ieltsFactsVersion": 2}
    reader = AsyncMock(return_value=fresh)
    service = SimpleNamespace(AsyncSessionLocal=store)
    for _ in range(2):
        result = await cache.cached_source(service, "thread", "turn", "owner", url,
            "https://www.aston.ac.uk", reader, 1, html_intake_schema=INTAKE_FACTS_VERSION)
        assert result["text"] == "Start date September 2027"
        assert result["_intakeFactsVersion"] == INTAKE_FACTS_VERSION
    reader.assert_awaited_once()
