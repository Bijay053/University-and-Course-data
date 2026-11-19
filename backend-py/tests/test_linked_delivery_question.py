from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.assistant_delivery_review import linked_delivery_question, handle_linked_delivery_question
from app.services.assistant_online_exclusion import selection, evidence

URL = "https://www.tru.ca/distance/programs/bachelor-of-arts-general-program.html"
PROMPT = f"{URL}\nthis course is online why it is not rejected\nclearly mention Delivery Online"
ROW = {"id": 9, "name": "Bachelor of Arts, General Program", "url": URL}


@pytest.mark.parametrize("prompt", [
    PROMPT, f"{URL}#fees why is this online course still pending?",
    f"{URL} why wasn't this distance course excluded?",
])
def test_short_url_complaints_are_delivery_checks(prompt):
    assert linked_delivery_question(prompt)
    assert selection(prompt, [ROW, {"id": 10, "name": "Other", "url": URL + "?option=campus"}]) == [ROW]


@pytest.mark.parametrize("prompt", [
    f"{URL} is this rejected?", f"{URL} why is it rejected? database-only",
    f"{URL} reject this online course", "why are online courses pending?",
    f"{URL} add this course", f"{URL} why is the fee missing?",
])
def test_other_intents_are_not_captured(prompt):
    assert not linked_delivery_question(prompt)


def test_explicit_url_does_not_select_a_mentioned_other_course():
    assert selection(f"{URL} unlike Other why is this online course pending?",
                     [ROW, {"id": 10, "name": "Other", "url": "https://other.edu/course"}]) == [ROW]
    assert selection(f"https://other.edu/course why is this online course pending?", [ROW]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [("Delivery Online", "checked"), (None, "mixed_mode"),
                                   (None, "unavailable"), (None, "unverified")])
async def test_question_reads_exact_source_without_mutation_or_old_action(monkeypatch, result):
    from datetime import datetime, timezone
    check = AsyncMock(return_value=result)
    monkeypatch.setattr("app.services.assistant_online_exclusion.check", check)
    service = SimpleNamespace(now=lambda: datetime.now(timezone.utc), save_research_reply=AsyncMock())
    assert await handle_linked_delivery_question(service, "t", "turn", "run",
                                                {"samples": [ROW], "home": "https://www.tru.ca"}, PROMPT)
    assert check.await_args.args[-2] == ROW
    reply = service.save_research_reply.await_args.args[-1]
    assert not reply["actions"]
    assert "row number" not in reply["content"]
    assert "No course records were changed" in reply["content"]
    assert bool(reply["sources"]) == (result[1] == "checked")
    if result[1] == "checked":
        assert "Delivery Online" in reply["content"]


def test_course_owned_delivery_label_proves_online_not_navigation():
    html = '<main><h1>Bachelor of Arts, General Program</h1><dl><dt>Delivery</dt><dd>Online</dd></dl></main>'
    assert evidence(html, URL)["state"] == "checked"
    chrome = '<nav>Delivery Online</nav><main><h1>Bachelor of Arts, General Program</h1></main>'
    assert evidence(chrome, URL)["state"] == "unverified"


def program_html(**overrides):
    import json
    data = {"url": URL[:-5], "credentialName": ROW["name"], "delivery": "distance"}
    data.update(overrides)
    return ('<header><h1></h1></header><main><h1>' + ROW["name"] + '</h1>'
            '<div class="info-delivery"><span class="info-title">Delivery</span>'
            '<span class="info-text"></span></div><p>'
            'The Bachelor of Arts, General program provides a broad education in Arts, '
            'with specialization in two subjects. Students complete the selected '
            'programme requirements and electives for this award.</p></main>'
            '<script>var arrCurrentProgram = ' + json.dumps(data) + ';</script>')


@pytest.mark.parametrize("change", [
    {"url": "https://www.tru.ca/programs/catalogue/bachelor-arts"},
    {"url": "https://evil.example/distance/programs/bachelor-of-arts-general-program"},
    {"credentialName": "Different Programme"}, {"delivery": "unknown"},
])
def test_unrelated_programme_data_is_not_materialized(change):
    from app.services.tru_delivery_source import materialize_delivery
    html = program_html(**change)
    assert materialize_delivery(html, URL) == html


def test_selected_data_not_related_programmes_and_empty_heading_is_safe():
    from app.services.tru_delivery_source import materialize_delivery
    html = program_html() + '<script>var arrPrograms = [{"delivery":"campus"}];</script>'
    rendered = materialize_delivery(html, URL)
    assert evidence(rendered, URL)["quote"] == "Delivery Online"
    assert materialize_delivery(html, "https://evil.example/programs/catalogue/test") == html
    assert materialize_delivery(html, URL + "?option=campus") == html
    assert materialize_delivery(html.replace("arrCurrentProgram", "arrRelatedPrograms"), URL).count("Online") == 0


@pytest.mark.asyncio
async def test_official_reader_retains_citable_selected_delivery():
    from app.services.scrape_assistant_tools import inspect_source, quote_in_source
    result = await inspect_source(URL, "https://www.tru.ca", raw_body=program_html().encode())
    assert result["verified"]
    assert result["_onlineExclusion"]["state"] == "checked"
    assert result["_onlineExclusionSchema"] == 3
    assert quote_in_source(result["_onlineExclusion"]["quote"], result)
