import copy
import pytest
from app.services.assistant_html_excerpts import MAX_CHARS
from app.services.scrape_assistant_tools import evidence_excerpts, quote_in_source


def fee_text():
    return (
        "Navigation tuition fees international IELTS " * 900
        + " MBA domestic Estimated domestic student tuition 2026-2027 "
        + "Estimated tuition total for the MBA program $34,929.54 "
        + "GDBA domestic tuition information. " * 90
        + " MBA international Estimated international student tuition 2026-2027 "
        + "All tuition fees are quoted in Canadian dollars. "
        + "Published estimated tuition is subject to change. " * 5
        + "Semester 1 tuition (3 courses) $8,989.02 Semester 2 tuition (3 courses) $8,989.02 "
        + "Estimated tuition total Year 1 $17,978.04 "
        + "Semester 1 (4 courses) $13,085.64 Semester 2 (4 courses) $13,085.64 "
        + "Semester 3 (4 courses) $13,085.64 Estimated tuition total Year 2 $39,256.92 "
        + "Continuation fee per semester $1,144.99 "
        + "Estimated tuition total for the MBA program $57,234.96 "
        + "Other programmes have different fees. " * 100
    )


def test_later_mba_total_retains_audience_year_currency_and_basis_verbatim():
    text=fee_text()
    windows=evidence_excerpts(text,[{"value":2996.34}])
    exact=next(w["text"] for w in windows if "$57,234.96" in w["text"])
    assert "MBA international" in exact
    assert "2026-2027" in exact and "Canadian dollars" in exact
    assert "tuition total for the MBA program" in exact
    assert "$34,929.54" not in exact
    assert all(quote_in_source(w["text"],{"_fullText":text}) for w in windows)
    assert sum(len(w["text"]) for w in windows)<=MAX_CHARS and len(windows)<=24
    assert len({w["id"] for w in windows})==len(windows)


@pytest.mark.asyncio
async def test_fee_request_refetches_missing_links_not_spliced_prefix_evidence():
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from app.services.assistant_source_cache import cached_source
    text=fee_text()
    source={"sourceType":"html","verified":True,"text":text[:24000],"_fullText":text,
            "_sourceCheckedAt":123,"sourceCapturedAt":"old","sourceOrigin":"official",
            "_sourceScopeVersion":2, "_ieltsFactsVersion":2,
            "_htmlFeeLinksSchema":0,
            "excerpts":[{"id":"q0","text":text[:800]}],"facts":[]}
    historic=copy.deepcopy(source)
    url="https://university.example/fees"
    thread=SimpleNamespace(messages=[{"_sourceChecks":{url:source}}])
    class DB:
        async def get(self,*args):return thread
        async def execute(self,*args):return SimpleNamespace(scalar_one_or_none=lambda:None)
    @asynccontextmanager
    async def sessions():yield DB()
    service=SimpleNamespace(AsyncSessionLocal=sessions)
    calls=[]
    async def inspect(*args):
        calls.append(args)
        return {**historic,"excerpts":evidence_excerpts(text,[]),"_htmlExcerptsSchema":1,
                "_htmlFeeLinksSchema":1}
    result=await cached_source(service,1,2,3,url,"https://university.example",inspect,5,
                               html_fee_links_schema=1)
    assert len(calls)==1
    assert any("$57,234.96" in w["text"] for w in result["excerpts"])
    assert source==historic
    assert result["_sourceCheckedAt"]==123 and result["sourceCapturedAt"]=="old"
    assert result["sourceOrigin"]=="official"
    thread.messages=[{"_sourceChecks":{url:result}}]
    cached=await cached_source(service,1,2,3,url,"https://university.example",inspect,5,
                               html_fee_links_schema=1)
    assert len(calls)==1 and cached["_sourceCacheHit"]


def test_pdf_layout_and_existing_quote_ids_stay_unchanged():
    text="IELTS 6.5\nListening 6.0\nReading 6.0\nWriting 6.0\nSpeaking 6.0"
    assert evidence_excerpts(text,[],preserve_layout=True)==[{"id":"q0","text":text}]
    short=evidence_excerpts("Official international tuition is CAD 20,000 annually.",[])
    assert short[0]["id"]=="q0"


def test_a_full_existing_quote_budget_does_not_discard_prior_evidence_ids():
    from app.services.assistant_html_excerpts import html_windows
    legacy=[{"id":f"q{n}","text":str(n).zfill(3)+"x"*797} for n in range(24)]
    assert html_windows(" ".join(q["text"] for q in legacy),legacy)==legacy


@pytest.mark.asyncio
@pytest.mark.parametrize("kind",["html","pdf"])
@pytest.mark.parametrize("padded",[False,True])
async def test_cache_reuse_keeps_history_age_and_pdf_ids_without_network(kind,padded):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from app.services.assistant_source_cache import cached_source
    text=fee_text().replace("Navigation tuition fees international IELTS " * 900,"Header. ")
    if padded:
        text=(text+"."*24000)[:24000]
    source={"sourceType":kind,"verified":True,"text":text,
            "_fullText":text+"\nUnrelated appended fragment $999,999.99",
            "_sourceScopeVersion":2,"_ieltsFactsVersion":2,"_sourceCheckedAt":123,
            "_htmlFeeLinksSchema":1,
            "excerpts":[{"id":"original-q0","text":text[:800]}],"facts":[]}
    historic=copy.deepcopy(source)
    url="https://university.example/fees"
    class DB:
        async def get(self,*args):
            return SimpleNamespace(messages=[{"_sourceChecks":{url:source}}])
    @asynccontextmanager
    async def sessions():yield DB()
    async def forbidden(*args):raise AssertionError("Cached evidence must not refetch")
    result=await cached_source(SimpleNamespace(AsyncSessionLocal=sessions),
        1,2,3,url,"https://university.example",forbidden,5)
    assert source==historic and result["_sourceCheckedAt"]==123
    assert result["excerpts"][0]["id"]=="original-q0"
    if kind=="html":
        assert any("$57,234.96" in q["text"] for q in result["excerpts"])
        assert all("$999,999.99" not in q["text"] for q in result["excerpts"])
    else:
        assert result["excerpts"]==source["excerpts"]


@pytest.mark.asyncio
async def test_owned_tuition_html_link_is_followable_but_navigation_and_foreign_are_not():
    from app.services.scrape_assistant_tools import inspect_source
    source=await inspect_source("https://www.tru.ca/programs/catalogue/mba.html",
        "https://www.tru.ca",raw_body=b"""
        <html><body><nav><a href="/navigation-tuition">Tuition</a></nav>
        <main><h1>Master of Business Administration</h1><h2>Program Fees</h2>
        <p>This programme provides postgraduate business education with full-time
        study on campus. Students should consult the programme's own published
        tuition guidance for the appropriate audience, academic year and fee basis.</p>
        <a href="/gaglardi/masters-degrees/admission-requirements/tuition.html#tab-3">MBA Tuition</a>
        <a href="https://evil.example/tuition">Tuition</a>
        <a href="/not-tuition.pdf">Application fees PDF</a></main></body></html>""")
    assert source["verified"]
    assert "https://www.tru.ca/gaglardi/masters-degrees/admission-requirements/tuition.html" in source["feeLinks"]
    assert not any("evil.example" in u or "navigation-tuition" in u for u in source["feeLinks"])
    assert source["_htmlFeeLinksSchema"]==1


@pytest.mark.asyncio
async def test_fee_link_cache_upgrade_requires_a_fee_investigation():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.services.assistant_source_cache import cached_source
    from tests.test_assistant_access_failures import Store
    url="https://university.example/course"
    store=Store()
    source={"url":url,"sourceType":"html","verified":True,"text":"Official course tuition information.",
            "_sourceScopeVersion":2,"_ieltsFactsVersion":2,"_sourceCheckedAt":1000,
            "facts":[],"excerpts":[{"id":"q0","text":"Official course tuition information."}],
            "feeLinks":[]}
    store.thread.messages=[{"role":"user","_sourceChecks":{url:copy.deepcopy(source)}}]
    service=SimpleNamespace(AsyncSessionLocal=store)
    reader=AsyncMock(side_effect=AssertionError("Unrequested link upgrade must not fetch"))
    result=await cached_source(service,"thread","turn","owner",url,"https://university.example",reader,1)
    assert result["_sourceCacheHit"]
    reader.assert_not_awaited()
    assert store.thread.messages[0]["_sourceChecks"][url]==source
    reader=AsyncMock(return_value={**source,"_htmlFeeLinksSchema":1,
                                  "feeLinks":["https://university.example/tuition"]})
    fresh=await cached_source(service,"thread","turn","owner",url,"https://university.example",
                              reader,1,html_fee_links_schema=1)
    reused=await cached_source(service,"thread","turn","owner",url,"https://university.example",
                               reader,1,html_fee_links_schema=1)
    reader.assert_awaited_once()
    assert fresh["feeLinks"]==reused["feeLinks"] and reused["_sourceCacheHit"]
