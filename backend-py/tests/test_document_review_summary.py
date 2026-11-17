from app.services.document_review_summary import summarize, describe
import pytest


def test_149_course_result_does_not_call_matched_unchanged_records_unmatched():
    rows = [
        {"status": "unchanged", "applicability": "verified"} for _ in range(75)
    ] + [{"status": "unverified"} for _ in range(63)] + [{"status": "excluded"} for _ in range(11)]
    rows[0]["bandConstraint"] = {"text": "One band at 5.5; others at 6.0."}
    result = summarize(rows)
    assert result == {"checked": 149, "notChecked": 0, "matched": 75, "unchanged": 75,
                      "proposed": 0, "unmatched": 74, "conditional": 1}
    content = describe(result, 149)
    assert "75 matched records" in content
    assert "74 remain unresolved or excluded" in content
    assert "There are no proposed changes to approve." in content
    assert "confirm the batch" not in content
    assert "149 remain unchanged" not in content


def test_unchecked_records_are_separate_from_unmatched_and_unchanged():
    counts = summarize([{"status": "not_checked"}, {"status": "proposed", "applicability": "verified"},
                        {"status": "conflict"}])
    assert counts["checked"] == 2 and counts["notChecked"] == 1
    assert counts["proposed"] == 1 and counts["unmatched"] == 1 and counts["unchanged"] == 0
    assert "confirm the batch" in describe(counts, 3)


def test_not_checked_and_unknown_bands_never_imply_all_fields_are_correct():
    counts = summarize([{"status": "unchanged", "applicability": "verified", "bandConstraint": {"text": "Combined rule"}},
                        {"status": "not_checked"}])
    content = describe(counts, 2)
    assert "no supported numeric change" in content
    assert "rather than four fixed skill scores" in content
    assert "1 could not be checked" in content


@pytest.mark.asyncio
async def test_full_review_reports_proposed_unchanged_and_protected_without_applying(monkeypatch):
    from contextlib import asynccontextmanager
    from copy import deepcopy
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from bs4 import BeautifulSoup
    from app.services import assistant_document_review as module, assistant_source_cache
    from app.services.ai import openai_client
    from app.services.english_html_tables import SCHEMA, profiles

    home = "https://uni.example"
    names = ["Aerospace Engineering BEng (Hons)", "Mechanical Engineering BEng (Hons)",
             "Nursing BSc (Hons)", "History BA (Hons)"]
    rows = [SimpleNamespace(id=i + 1, university_id=1, status="pending", course_name=name,
                            course_website=f"{home}/courses/{i}", degree_level="Bachelor",
                            international_fee=None, currency=None, fee_term=None, fee_year=None,
                            duration=None, duration_term=None, course_location=None, intake_months=None, study_mode=None,
                            **{f: (6.0 if f == "ielts_overall" else 5.5) if i == 0 else None for f in module.BANDS})
            for i, name in enumerate(names)]
    before = deepcopy([vars(r) for r in rows])
    header = "<tr><th>Programme</th><th>Reading</th><th>Writing</th><th>Listening</th><th>Speaking</th><th>Overall</th></tr>"
    html = "<dl><dt>IELTS Undergraduate</dt><dd><table>" + header + "".join(
        f"<tr><th>{name}</th>" + "<td>5.5</td>" * 4 + "<td>6.0</td></tr>" for name in names[:2]
    ) + "</table></dd></dl>"
    guidance = {"url": home + "/english", "verified": True, "sourceType": "html",
                "_fullText": BeautifulSoup(html, "html.parser").get_text(" ", strip=True),
                "_englishTablesSchema": SCHEMA, "_englishTables": profiles(html)}

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: rows))))

    service = module.service
    monkeypatch.setattr(service, "AsyncSessionLocal", session)
    monkeypatch.setattr(service, "context", AsyncMock(return_value={
        "university_id": 1, "eligible_ids": [r.id for r in rows], "home": home, "config": None}))
    monkeypatch.setattr(service, "progress", AsyncMock())
    monkeypatch.setattr(openai_client, "_client", lambda: (_ for _ in ()).throw(
        AssertionError("Structured missing scope must not cause a paid model retry")))
    saved = AsyncMock()
    monkeypatch.setattr(service, "save_research_reply", saved)
    monkeypatch.setattr(assistant_source_cache, "cached_source", AsyncMock(return_value=guidance))
    pages = {r.course_website: {"url": r.course_website, "verified": True, "sourceType": "html",
                              "courseTitle": r.course_name, "_fullText": r.course_name, "facts": []} for r in rows}
    monkeypatch.setattr(module, "inspect_source", AsyncMock(side_effect=lambda url, home: pages[url]))
    await module.review("thread", "turn", "run", SimpleNamespace(job_id="job", actor_id=1),
                        {"content": home + "/english update IELTS for all pending courses"})
    reply = saved.call_args.args[3]
    report = reply["documentReview"]
    assert (report["checked"], report["matched"], report["proposed"], report["unchanged"], report["unmatched"]) == (4, 2, 1, 1, 2)
    assert [r["status"] for r in report["rows"]] == ["unchanged", "proposed", "unverified", "unverified"]
    assert len(report["rows"][1]["changes"]) == 5
    assert reply["actions"][0]["requiresConfirmation"] is True
    assert reply["actions"][0]["_applicabilityVersion"] == 2
    assert [t["id"] for t in reply["actions"][0]["_targets"]] == [2]
    assert [vars(r) for r in rows] == before


@pytest.mark.asyncio
async def test_old_html_proposals_are_rejected_before_accessing_or_writing_courses():
    from types import SimpleNamespace
    from app.services import assistant_document_review as module
    with pytest.raises(ValueError, match="Run the document check again"):
        await module.apply(SimpleNamespace(), SimpleNamespace(turn_id="turn"),
                           {"_turnId": "turn", "_applicabilityVersion": 1, "_sourceType": "html"},
                           1, {"id": 1})
