from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bs4 import BeautifulSoup

from app.services.scraper.extractors import aru_linked_english as aru
from app.services.scraper.extractors import english_test
from app.services.scraper.requirement_status import build_requirement_status

POLICY = (
    "English language requirements Standard English requirements "
    "The minimum requirements for most of our degrees are provided in the table below. "
    "Some of our courses have higher requirements, so please visit the course page "
    "you are interested in to check the specific English requirement. "
    "Undergraduate courses IELTS (Academic) Overall 6.0 with 5.5 in each of the four "
    "language skills (listening, speaking, reading and writing) TOEFL iBT 80 overall "
    "Postgraduate courses IELTS (Academic) Overall 6.5 with 5.5 in each of the four "
    "language skills (listening, speaking, reading and writing) TOEFL iBT 88 overall"
)


def page(name="Banking and Finance", award="MBA", level="postgraduate", english=None):
    referral = (
        "<p>If English is not your first language, you'll need to make sure you meet our "
        f'<a href="/international/entry-requirements">English language requirements for {level} courses</a>.</p>'
        "<p>Check the section '<strong>English language requirements</strong>' for IELTS requirements.</p>"
    )
    return (
        f"<html><head><title>{name} - {award} - ARU</title></head><body>"
        f"<nav><a href='{aru.POLICY_URL}'>English language requirements for undergraduate courses</a></nav>"
        f"<main><h1>{name}</h1><h3>Foundation entry alternatives</h3>"
        "<p>At least IELTS 5.5 (Academic level) or equivalent English Language qualification.</p>"
        f"<h3>English language requirements</h3><div class='rte'>{referral if english is None else english}</div>"
        "</main></body></html>"
    )


URL = "https://www.aru.ac.uk/study/postgraduate/banking-and-finance-mba"
UG_URL = "https://www.aru.ac.uk/study/undergraduate/artificial-intelligence"


@pytest.mark.parametrize("level,overall", [("undergraduate", 6), ("postgraduate", 6.5)])
def test_actual_policy_keeps_levels_and_all_four_skills_separate(level, overall):
    scores, quote = aru.policy_values(POLICY, level)
    assert scores == {"ielts_overall": overall, **{
        "ielts_" + part: 5.5 for part in ("listening", "reading", "writing", "speaking")}}
    assert quote in POLICY
    assert quote.startswith(level.capitalize() + " courses")


@pytest.mark.asyncio
@pytest.mark.parametrize("overall", [6, 6.5])
async def test_four_language_skills_have_a_literal_score_quote_not_navigation(overall):
    paragraph = (
        f"IELTS (Academic) Overall {overall} with 5.5 in each of the four language skills "
        "(listening, speaking, reading and writing)"
    )
    result = await english_test.extract(
        "<html><title>Requirements</title><nav>" + "Navigation " * 400
        + "</nav><main><p>" + paragraph + "</p></main></html>", aru.POLICY_URL)
    ielts = next(item for item in result if item.field_key == "ielts_overall")
    assert ielts.normalized["ielts_overall"] == overall
    assert all(ielts.normalized[field] == 5.5 for field in aru.FIELDS[1:])
    assert paragraph in ielts.snippet
    assert len(ielts.snippet) <= 1200


@pytest.mark.asyncio
async def test_degree_does_not_borrow_foundation_alternative_ielts():
    result = await english_test.extract(page("Artificial Intelligence degree", "BSc (Hons)",
                                            "undergraduate"), UG_URL)
    assert not any(item.field_key.startswith("ielts_") for item in result)
    assert aru.referral(page("Artificial Intelligence degree", "BSc (Hons)",
                             "undergraduate"), UG_URL)["level"] == "undergraduate"


@pytest.mark.parametrize("name,award", [
    ("Adult Nursing", "BSc"), ("Clinical Psychology", "MSc"),
    ("Applied Biomedical Science", "BSc"), ("Education research", "MA"),
    ("International Business MPhil", "MSc"), ("Teacher Training", "PGCert"),
    ("Optometry", "BSc"), ("Medicine", "MBA"), ("Computer Science Foundation", "BSc"),
    ("Social Work", "MA"), ("Counselling", "MA"),
    ("Robotic Surgery", "MSc"), ("Sports Therapy", "BSc"),
    ("Operating Department Practice", "BSc"), ("Applied Professional Practice", "MSc"),
    ("Life Sciences", "MRes"),
    ("Architecture", "MArch"),
])
def test_professional_research_and_pathway_referrals_are_not_standard_defaults(name, award):
    assert aru.referral(page(name, award), URL) is None


@pytest.mark.parametrize("url", [
    URL.replace("www.aru.ac.uk", "aru.ac.uk.evil.example"),
    URL.replace("https:", "http:"), URL + "/another-programme",
    "https://www.aru.ac.uk/international/entry-requirements",
])
def test_unrelated_and_non_course_urls_cannot_authorize_a_standard_rule(url):
    assert aru.referral(page(), url) is None


def test_mismatched_level_nav_only_and_wrong_official_guidance_are_rejected():
    assert aru.referral(page(level="undergraduate"), URL) is None
    assert aru.referral(page().replace('class="rte"', 'class="other"').replace(
        "class='rte'", "class='other'"), URL) is None
    assert aru.referral(page().replace('href="/international/entry-requirements"',
                                       'href="/international/english-language-support"'), URL) is None
    assert aru.referral(page().replace("<h1>", "<h2>").replace("</h1>", "</h2>"), URL) is None
    assert aru.referral(page().replace(
        "<title>", '<link rel="canonical" href="https://www.aru.ac.uk/study/postgraduate/another-mba"><title>'),
        URL) is None
    header = (
        "<header><div class='rte'>If English is not your first language, meet our "
        f"<a href='{aru.POLICY_URL}'>English language requirements for postgraduate courses</a>. "
        "Check IELTS requirements.</div></header>"
    )
    assert aru.referral(page(english="<p>Not published.</p>").replace("<main>", header + "<main>"), URL) is None


@pytest.mark.asyncio
async def test_explicit_course_exception_is_not_replaced_by_a_standard_policy():
    own = "<p>IELTS 7.0 overall with no band below 6.5.</p>"
    html = page(english=own)
    assert aru.referral(html, URL) is None
    fetch = AsyncMock()
    assert not await aru.recover(html, URL, {}, [], fetch)
    fetch.assert_not_awaited()
    parsed = await english_test.extract(html, URL)
    fact = next(item for item in parsed if item.field_key == "ielts_overall")
    assert fact.normalized["ielts_overall"] == 7
    assert fact.normalized["ielts_writing"] == 6.5
    research = page("Business Research", "PhD", english=own)
    research_parsed = await english_test.extract(research, URL)
    assert next(item for item in research_parsed if item.field_key == "ielts_overall").value == 7


def test_conflicting_repeated_or_conditional_policy_cannot_authorize_a_rule():
    assert aru.policy_values(POLICY + " " + POLICY, "postgraduate") is None
    changed = POLICY.replace("writing) TOEFL iBT 88", "writing) except Writing requires 6.5. TOEFL iBT 88")
    assert aru.policy_values(changed, "postgraduate") is None
    assert aru.policy_values(POLICY.replace("5.5", "7.0"), "undergraduate") is None
    assert aru.policy_values(POLICY, "doctorate") is None


def test_four_skill_proof_cannot_verify_a_component_that_has_a_named_exception():
    from app.services.scraper.requirement_status import _component_evidence_matches
    quote = ("IELTS (Academic) Overall 6.5 with 5.5 in each of the four language skills "
             "(listening, speaking, reading and writing), except Writing requires 6.5.")
    assert not _component_evidence_matches("ielts_writing", 5.5, quote)


@pytest.mark.asyncio
async def test_recovery_preserves_unrelated_values_and_builds_actual_verified_status():
    aru._POLICIES.clear()
    payload = {"course_name": "Banking and Finance - MBA", "ielts_overall": 6,
               "international_fee": 18000, "fee_year": 2027, "intake_months": ["September"],
               "course_location": "Cambridge", "status": "pending", "academic": "unchanged",
               "extraction_method": {"ielts_overall": "uni_config:english_default",
                                     "international_fee": "css:course_fee"}}
    before = deepcopy(payload)
    evidence = [{"field_key": "international_fee", "value": 18000},
                {"field_key": "ielts_overall", "value": 6, "method": "uni_config:english_default"}]
    fetch = AsyncMock(return_value="<main>" + POLICY + "</main>")
    assert await aru.recover(page(), URL, payload, evidence, fetch)
    assert {k: v for k, v in payload.items() if k not in (*aru.FIELDS, "extraction_method")} == {
        k: v for k, v in before.items() if k not in (*aru.FIELDS, "extraction_method")}
    assert payload["extraction_method"]["international_fee"] == "css:course_fee"
    assert all(payload["extraction_method"][field] == "english.aru_linked_central:table"
               for field in aru.FIELDS)
    assert payload["ielts_overall"] == 6.5
    assert all(payload[field] == 5.5 for field in aru.FIELDS[1:])
    selected = [item for item in evidence if item["field_key"] in aru.FIELDS]
    assert len(selected) == 5
    assert all(item["snippet"] in POLICY and item["source_url"] == aru.POLICY_URL for item in selected)
    for item in selected:
        item["decision_status"] = "selected"
    status = build_requirement_status(SimpleNamespace(**payload), evidence=evidence, source_url=URL)
    assert status["englishComponents"]["state"] == "verified"
    fetch.assert_awaited_once_with(aru.POLICY_URL)


@pytest.mark.asyncio
async def test_concurrent_courses_share_one_bounded_policy_read_and_keep_exception_unchanged():
    aru._POLICIES.clear()
    fetch = AsyncMock(return_value="<main>" + POLICY + "</main>")
    import asyncio
    records = [{"course_name": "Banking and Finance - MBA"} for _ in range(8)]
    outcomes = await asyncio.gather(*(aru.recover(page(), URL, row, [], fetch) for row in records))
    assert all(outcomes)
    fetch.assert_awaited_once()
    assert aru._POLICIES[asyncio.get_running_loop()]["task"] is None
    untouched = {"course_name": "Adult Nursing - BSc", "ielts_overall": 7}
    assert not await aru.recover(page("Adult Nursing", "BSc"), URL, untouched, [], fetch)
    assert untouched == {"course_name": "Adult Nursing - BSc", "ielts_overall": 7}


@pytest.mark.asyncio
async def test_access_failure_cannot_change_fields_or_be_retried_for_each_course():
    aru._POLICIES.clear()
    fetch = AsyncMock(side_effect=RuntimeError("HTTP 403: source unavailable"))
    row = {"ielts_overall": 6}
    evidence = []
    with pytest.raises(RuntimeError, match="403"):
        await aru.recover(page(), URL, row, evidence, fetch)
    assert not await aru.recover(page(), URL, row, evidence, fetch)
    assert row == {"ielts_overall": 6} and evidence == []
    fetch.assert_awaited_once()


def preview_sources():
    html = page()
    return [
        {"url": URL, "verified": True, "sourceType": "html", "facts": [],
         "_aruEnglishScopeVersion": aru.SCHEMA, "_aruEnglishReferral": aru.referral(html, URL),
         "text": BeautifulSoup(html, "html.parser").get_text(" ", strip=True)},
        {"url": aru.POLICY_URL, "verified": True, "sourceType": "html", "text": POLICY},
    ]


def test_preview_shows_all_five_old_and_new_values_without_changing_records():
    from app.services.assistant_repair_proposals import finalize_field_proposal
    row = {"id": 1, "name": "Banking and Finance - MBA", "url": URL,
           "ielts": 6, "ieltsBands": {}, "status": "pending"}
    original = deepcopy(row)
    action = {"kind": "fix_fields", "fields": ["ielts_overall"], "_ids": [1]}
    finalize_field_proposal(action, "verify IELTS", {"samples": [row]}, preview_sources())
    preview = action["repairPreview"][0]
    assert preview["status"] == "supported" and len(preview["changes"]) == 5
    assert preview["changes"][0] == {"field": "ielts_overall", "oldValue": 6, "newValue": 6.5}
    assert all(change["oldValue"] is None and change["newValue"] == 5.5 for change in preview["changes"][1:])
    assert action["requiresConfirmation"]
    assert row == original


@pytest.mark.parametrize("failure", ["name", "level", "policy", "blocked", "scope", "joined", "research"])
def test_uncertain_course_matching_never_produces_a_preview(failure):
    row = {"name": "Banking and Finance - MBA", "url": URL}
    sources = preview_sources()
    if failure == "name":
        row["name"] = "Banking and Finance - MSc"
    elif failure == "level":
        sources[0]["_aruEnglishReferral"]["level"] = "doctorate"
    elif failure == "policy":
        sources[1]["url"] += "/foreign"
    elif failure == "blocked":
        sources[1]["verified"] = False
    elif failure == "scope":
        sources[0]["_aruEnglishScopeVersion"] = 0
    elif failure == "joined":
        sources[1]["text"] = "General university information without requirements."
        sources[1]["_fullText"] = POLICY
    elif failure == "research":
        row["name"] = "Banking and Finance - MBA (Research)"
    assert aru.comparison(row, sources[0], sources) == []


@pytest.mark.asyncio
async def test_real_source_reader_preserves_the_owned_referral_as_a_contiguous_excerpt(monkeypatch):
    from app.services import scrape_assistant_tools as tools
    from app.services.scraper.extractors import fee
    html = page().replace("<h3>Foundation entry alternatives</h3>",
                          "<p>" + "Course information. " * 1800 + "</p><h3>Foundation entry alternatives</h3>")
    monkeypatch.setattr(tools, "public_request", AsyncMock(return_value=html.encode()))
    monkeypatch.setattr(fee, "extract", AsyncMock(return_value=[]))
    source = await tools.inspect_source(URL, "https://www.aru.ac.uk")
    assert source["verified"]
    scope = source["_aruEnglishReferral"]
    assert scope and source["_aruEnglishScopeVersion"] == aru.SCHEMA
    assert scope["quote"] not in source["text"]
    assert any(scope["quote"] in item["text"] for item in source["excerpts"])
    assert not any(item["field"] == "ielts_overall" for item in source["facts"])
    row = {"name": "Banking and Finance - MBA", "url": URL}
    assert aru.comparison(row, source, [
        {"url": aru.POLICY_URL, "verified": True, "sourceType": "html", "text": POLICY}])
