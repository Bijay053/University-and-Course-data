from unittest.mock import AsyncMock
from types import SimpleNamespace
import asyncio
import pytest

from app.services.scraper.extractors import tru_fee_recovery as module
from app.services.scraper.extractors import tru_calculator
from app.services import official_source_redirects
from tests.test_scrape_prompt_assistant import store, assistant_postgres

URL = "https://www.tru.ca/programs/catalogue/bachelor-journalism.html"


@pytest.mark.asyncio
async def test_recovery_passes_the_exact_selected_fee_and_evidence_to_the_existing_writer(monkeypatch):
    read = AsyncMock(return_value=(b"<main>official</main>", URL, []))
    monkeypatch.setattr(official_source_redirects, "read_official_source", read)
    fee = SimpleNamespace(
        field_key="international_fee", value=8860.56,
        normalized={"international_fee":8860.56, "fee_term":"Semester", "currency":"CAD", "fee_year":None},
        method="fee.tru_international_calculator_four_courses",
        snippet="International tuition 2215.14 per course × 4 courses", confidence=1.0,
    )
    monkeypatch.setattr(tru_calculator, "extract", AsyncMock(return_value=(True, fee)))
    result = await module.recover(URL)
    assert result["payload"]["international_fee"] == 8860.56
    assert result["payload"]["fee_term"] == "Semester"
    assert result["payload"]["fee_year"] is None
    assert result["evidence"][0]["source_url"] == URL
    assert result["evidence"][0]["decision_status"] == "selected"
    assert result["evidence"][0]["snippet"] == fee.snippet
    assert not any(key.startswith("ielts") for key in result["payload"])
    read.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ValueError("robots disallows source"), TimeoutError("read timed out")])
async def test_source_failure_never_returns_an_amount_or_bypasses_access(monkeypatch, failure):
    read = AsyncMock(side_effect=failure)
    monkeypatch.setattr(official_source_redirects, "read_official_source", read)
    result = await module.recover(URL)
    assert result["status"] == "failed" and "payload" not in result
    read.assert_awaited_once()


@pytest.mark.asyncio
async def test_other_providers_and_distance_fees_are_not_given_a_generic_semester_price(monkeypatch):
    assert await module.recover("https://www.other.edu/programs/catalogue/x.html") is None
    monkeypatch.setattr(official_source_redirects, "read_official_source", AsyncMock(
        return_value=(b"<main>No calculator</main>", "https://www.tru.ca/distance/programs/example.html", [])))
    assert await module.recover("https://www.tru.ca/distance/programs/example.html") is None


@pytest.mark.asyncio
async def test_only_publisher_declared_same_programme_redirect_can_reach_normal_distance_extraction(monkeypatch):
    stub = "https://www.tru.ca/distance/programs/example/"
    final = "https://www.tru.ca/distance/programs/example.html"
    raw = b'<meta http-equiv="refresh" content="1; url=/distance/programs/example.html">'
    read = AsyncMock(side_effect=[(raw, stub, []), (b"<main>Programme</main>", final, [])])
    monkeypatch.setattr(official_source_redirects, "read_official_source", read)
    assert await module.recover(stub) == {"_official_course_url": final}
    assert read.await_count == 2
    assert module.publisher_move(raw.replace(b"example.html", b"another.html"), stub) is None
    with pytest.raises(ValueError):
        module.publisher_move(b'<meta http-equiv="refresh" content="1; url=https://evil.example/file.html">', stub)


def test_identical_repeated_asset_links_are_not_conflicting_fee_schedules():
    from tests.test_tru_fee_calculator import HTML
    # Some real catalogue pages include the same schedule script twice.
    import re
    match = re.search(r'<script[^>]+src=["\'][^"\']*academic\d+\.js["\'][^>]*></script>', HTML)
    assert match
    duplicated = HTML + match[0]
    assert tru_calculator.assets(duplicated, URL) == tru_calculator.assets(HTML, URL)
    ambiguous = duplicated + '<script src="/__shared/assets/academic99999.js"></script>'
    assert tru_calculator.assets(ambiguous, URL) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("distance", [False, True])
async def test_pending_fee_fix_persists_calculator_amount_basis_currency_and_evidence(store, monkeypatch, distance, tmp_path):
    from app.models import ScrapedCourse, University
    from app.routers import scrape
    from app.services.scraper import central_pages
    from app.services.scraper import orchestrator
    from app.services.scraper.config import loader
    from app.models.evidence import ScrapedFieldEvidence
    from sqlalchemy import select
    monkeypatch.setattr(loader, "_UNIS_DIR", tmp_path / "unis")
    monkeypatch.setattr(loader, "_RUNTIME_UNIS_DIR", tmp_path / "runtime_unis")
    source = URL if not distance else "https://www.tru.ca/distance/programs/example/"
    final = URL if not distance else "https://www.tru.ca/distance/programs/example.html"
    amount, basis = (8860.56, "Semester") if not distance else (275, "Per Credit")
    quote = (f"International tuition CAD {amount} " + ("per semester: 4 courses" if not distance else "per credit"))
    fee = {"status": "success", "url": URL,
           "payload": {"international_fee":amount, "currency":"CAD", "fee_term":basis, "fee_year":None},
           "evidence": [{"field_key":"international_fee", "value":amount, "source_url":final,
                         "method":"fee.tru_international_calculator_four_courses" if not distance else "fee.table",
                         "snippet":quote,
                         "decision_status":"selected", "confidence":1.0}]}
    fee["url"] = final
    monkeypatch.setattr(module, "recover", AsyncMock(return_value=fee if not distance else {"_official_course_url":final}))
    extraction = AsyncMock(return_value=fee) if distance else AsyncMock(side_effect=AssertionError("No unrelated AI extraction"))
    monkeypatch.setattr(orchestrator, "_extract_only", extraction)
    monkeypatch.setattr(central_pages, "prefetch_central_pages", AsyncMock(return_value={}))
    async with store() as db:
        uni = await db.get(University, 1)
        uni.scrape_url = "https://www.tru.ca"
        uni.name = "Thompson Rivers University"
        uni.country = "Canada"
        row = await db.get(ScrapedCourse, 1)
        row.course_website, row.course_name = source, "Bachelor of Journalism"
        original = row.ielts_overall
        await db.commit()
        result = await scrape.re_extract_staged(scrape.ReExtractBody(
            ids=[1], universityId=1, targetFields=["international_fee"], smart=True), db)
        assert result["results"][0]["ok"], result
        await db.refresh(row)
        assert (row.international_fee, row.currency, row.fee_term, row.fee_year) == (amount, "CAD", basis, None)
        assert row.course_website == source  # Fee repair cannot rewrite another field.
        assert row.ielts_overall == original
        evidence = (await db.execute(select(ScrapedFieldEvidence).where(
            ScrapedFieldEvidence.scraped_course_id==1,
            ScrapedFieldEvidence.field_key=="international_fee",
            ScrapedFieldEvidence.selected.is_(True)))).scalars().all()
        assert any(e.source_url==final and str(amount) in e.snippet for e in evidence)
        if distance:
            assert extraction.await_args.args[0]["url"] == final
        else:
            extraction.assert_not_awaited()


@pytest.mark.asyncio
async def test_slow_first_course_does_not_consume_later_courses_time(monkeypatch):
    from app.services.scraper import smart_fix
    from app.routers import scrape
    from tests.test_smart_fix import analysis, body
    monkeypatch.setattr(scrape, "analyze_staged", AsyncMock(return_value=analysis("international_fee")))
    monkeypatch.setattr(scrape, "re_extract_staged", AsyncMock(return_value={"results":[{"id":2,"ok":True}]}))
    original = asyncio.wait_for
    allocations = []
    async def wait(coro, timeout):
        allocations.append(timeout)
        if len(allocations) == 1:
            coro.close()
            raise TimeoutError()
        return await original(coro, timeout)
    monkeypatch.setattr(smart_fix.asyncio, "wait_for", wait)
    db = SimpleNamespace(rollback=AsyncMock())
    result = await smart_fix.run_smart_batch(body(ids=[1,2], targets=["international_fee"]), db)
    assert 0 < allocations[0] <= 120 and allocations[1] > allocations[0]
    assert result["results"][0]["reason_code"] == "budget_exhausted"
    assert result["results"][1]["attempted"] is True
