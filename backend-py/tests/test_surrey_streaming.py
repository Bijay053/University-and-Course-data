"""Deterministic Surrey streaming handoff tests; no network, AI or shared DB."""
import asyncio
from contextlib import aclosing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.scraper import orchestrator as orch
from app.services.scraper.extractors import uk_course_facts as facts


BASE = "https://www.surrey.ac.uk/undergraduate/"


def page(title):
    return (
        f'<h1><span class="t-page-title">{title}</span>'
        '<span>BSc (Hons) — 2027 entry</span></h1>'
        + "".join(
            f'<a href="#option-{key}">{award}</a><div id="option-{key}">'
            '<dl><dt>Full time</dt><dd>3 years</dd></dl>'
            '<dl><dt>Campus</dt><dd>Stag Hill</dd></dl></div>'
            for key, award in (("standard", "BSc (Hons)"),
                               ("placement", "BSc (Hons) with placement"))
        )
    )


def pipeline(links, fetch, on_prepared=None, **kwargs):
    return orch._iter_surrey_extraction_batches(
        links, kwargs.pop("max_courses", 20), fetch=fetch,
        parallel=kwargs.pop("parallel", 2), batch_size=kwargs.pop("batch_size", 50),
        on_progress=kwargs.pop("on_progress", AsyncMock()),
        stop_flag=kwargs.pop("stop_flag", [False]),
        on_prepared=on_prepared or AsyncMock(side_effect=lambda group: group),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_fast_source_is_extracted_and_staged_before_slow_source_finishes():
    slow_started = asyncio.Event()
    release = asyncio.Event()
    calls = []
    events = []
    staged = {}
    async def fetch(url):
        calls.append(url)
        if url.endswith("slow"):
            slow_started.set()
            await release.wait()
        else:
            await slow_started.wait()
        return page(url.rsplit("/", 1)[-1])
    async def observe(**event):
        events.append(event)
    links = [{"url": BASE + "slow"}, {"url": BASE + "fast"}]
    async with aclosing(pipeline(links, fetch, on_progress=observe)) as stream:
        first = await asyncio.wait_for(anext(stream), 2)
        assert all("/fast?" in link["url"] for link in first)
        assert not release.is_set()
        # Actual option-owned authority parses the exact shared source document;
        # no fetch is needed between the handoff and staging.
        assert first[0]["_surrey_html"] is first[1]["_surrey_html"]
        for link in first:
            payload = {}
            facts.apply_authority(link["_surrey_html"], link["url"], payload, [])
            staged[link["url"]] = payload
        assert len(staged) == 2
        assert [e["completed"] for e in events if e["kind"] == "course_prepare_done"] == [1]
        release.set()
        async for batch in stream:
            for link in batch:
                assert link["url"] not in staged
                staged[link["url"]] = {}
    assert len(staged) == 4 and len(calls) == 2
    assert {e["completed"] for e in events if e["kind"] == "course_prepare_done"} == {1, 2}


@pytest.mark.asyncio
async def test_out_of_order_resume_matches_exact_options_and_preserves_review_rows():
    # A parent checkpoint must not hide either option. A reviewed option must
    # not be overwritten, while its placement sibling still enters staging.
    parent = BASE + "fast"
    standard = facts.option_url(parent, "option-standard")
    reviewed = {standard: {"id": 41, "status": "pending", "staff_value": 123}}
    original = dict(reviewed[standard])
    checkpoints = {
        orch._normalize_course_url(parent): (40, "old"),
        orch._normalize_course_url(standard): (41, "old"),
    }
    provenance = []
    async def resume(group):
        keys, ids, jobs = orch._matched_resume_provenance(group, checkpoints)
        provenance.extend(ids)
        assert jobs in ([], ["old"])
        return [link for link in group if orch._normalize_course_url(link["url"]) not in keys]
    # Parent + duplicate parent + explicit standard option must still stage
    # only the remaining distinct option, irrespective of completion order.
    sources = [{"url": parent}, {"url": parent}, {"url": standard}]
    async with aclosing(pipeline(sources, AsyncMock(return_value=page("Fast")), resume)) as stream:
        async for batch in stream:
            for link in batch:
                assert link["url"] not in reviewed
                reviewed[link["url"]] = {"status": "pending"}
    assert reviewed[standard] == original
    assert len(reviewed) == 2
    assert provenance == [41]


@pytest.mark.asyncio
async def test_backpressure_bounds_prepared_documents_and_batch_size():
    calls = []
    async def fetch(url):
        calls.append(url)
        return page(url)
    links = [{"url": BASE + str(i)} for i in range(10)]
    async with aclosing(pipeline(links, fetch, max_courses=20, batch_size=1)) as stream:
        first = await asyncio.wait_for(anext(stream), 2)
        await asyncio.sleep(.02)
        assert len(first) == 1
        assert len(calls) <= 2, "Consumer staging must backpressure preparation"
        batches = [first]
        async for batch in stream:
            batches.append(batch)
    assert len(batches) == 20
    assert len({batch[0]["url"] for batch in batches}) == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_kind", ["close", "task", "stop"])
async def test_cancellation_drains_preparation_and_does_not_stage_again(cancel_kind):
    blocked = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    stop = [False]
    staged = []
    async def fetch(url):
        if url.endswith("slow"):
            blocked.set()
            try:
                await release.wait()
            finally:
                cancelled.set()
        else:
            await blocked.wait()
        return page(url)
    stream = pipeline([{"url": BASE + "fast"}, {"url": BASE + "slow"}],
                      fetch, stop_flag=stop)
    try:
        staged.extend(await asyncio.wait_for(anext(stream), 2))
        if cancel_kind == "close":
            await stream.aclose()
        else:
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(.02)
            if cancel_kind == "task":
                pending.cancel()
            else:
                stop[0] = True
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 2)
        assert cancelled.is_set()
        assert len(staged) == 2
        assert len({link["url"] for link in staged}) == 2
    finally:
        release.set()
        await stream.aclose()


@pytest.mark.asyncio
async def test_late_cap_failure_does_not_truncate_or_delete_staged_work():
    release = asyncio.Event()
    staged = []
    async def fetch(url):
        if url.endswith("slow"):
            await release.wait()
        return page(url)
    async with aclosing(pipeline(
        [{"url": BASE + "fast"}, {"url": BASE + "slow"}], fetch, max_courses=3,
    )) as stream:
        staged.extend(await asyncio.wait_for(anext(stream), 2))
        release.set()
        with pytest.raises(ValueError, match="not truncated"):
            await anext(stream)
    assert len(staged) == 2  # Previously staged review records remain available.


@pytest.mark.asyncio
async def test_source_cap_is_checked_before_any_network_calls():
    fetch = AsyncMock()
    async with aclosing(pipeline(
        [{"url": BASE + str(i)} for i in range(3)], fetch, max_courses=2,
    )) as stream:
        with pytest.raises(ValueError, match="source count"):
            await anext(stream)
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_preparation_and_extraction_share_the_same_concurrency_gate():
    gate = asyncio.Semaphore(2)
    active = peak = 0
    async def network():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(.01)
        active -= 1
    async def fetch(url):
        await network()  # Preparation acquires the shared gate in the provider.
        return page(url)
    async def extract(link):
        async with gate:
            await network()
    async with aclosing(pipeline(
        [{"url": BASE + str(i)} for i in range(5)], fetch, fetch_gate=gate,
    )) as stream:
        async for batch in stream:
            await asyncio.gather(*(extract(link) for link in batch))
    assert peak <= 2 and active == 0


@pytest.mark.asyncio
async def test_collector_preserves_interleaved_duplicate_input_order():
    links = [{"url": BASE + "a", "marker": 1},
             {"url": BASE + "b", "marker": 2},
             {"url": BASE + "a", "marker": 3}]
    fetch = AsyncMock(return_value=page("Course"))
    result = await facts.expand_links(links, 10, fetch, parallel=2)
    assert [link["marker"] for link in result] == [1, 1, 2, 2, 3, 3]
    assert fetch.await_count == 2


def test_prepared_group_metadata_does_not_count_unextracted_options_as_processed():
    parent = BASE + "course"
    standard = facts.option_url(parent, "option-standard")
    placement = facts.option_url(parent, "option-placement")
    source_html = page("Course")
    group = [{"url": standard, "_surrey_html": source_html},
             {"url": placement, "_surrey_html": source_html}]
    job = SimpleNamespace(
        total_found=5, current=0, request_payload={"resumeCourseIds": [17],
            "resumeSourceJobIds": ["earlier"], "unrelated": True},
        discovered_config={"pipeline_stats": {"source_candidates": 5},
                           "other_evidence": "preserved"},
    )
    links = []
    # A legacy parent URL is not equivalent to either selected option.
    checkpoints = {orch._normalize_course_url(parent): (19, "parent")}
    remaining, skipped, replaced = orch._record_surrey_prepared_group(
        job, group, links=links, checkpoints=checkpoints, pending_rows={},
        source_count=5,
    )
    assert remaining == group and skipped == 0 and replaced == set()
    assert job.current == 0 and job.total_found == 5
    assert job.discovered_config["pipeline_stats"] == {
        "source_candidates": 5, "expanded_course_options": 2,
    }
    assert job.discovered_config["other_evidence"] == "preserved"
    assert all("_surrey_html" not in link for link in links)
    assert remaining[0]["_surrey_html"] is remaining[1]["_surrey_html"]

    # Resume increments are returned separately; preparation still must not
    # increment the extraction counter or erase earlier review provenance.
    checkpoints[orch._normalize_course_url(standard)] = (23, "reviewed")
    remaining, skipped, replaced = orch._record_surrey_prepared_group(
        job, group, links=[], checkpoints=checkpoints,
        pending_rows={orch._normalize_course_url(placement): [25]},
        source_count=5,
    )
    assert [link["url"] for link in remaining] == [placement]
    assert skipped == 1 and replaced == {25} and job.current == 0
    assert job.request_payload == {
        "resumeCourseIds": [17, 23],
        "resumeSourceJobIds": ["earlier", "reviewed"],
        "unrelated": True,
    }


@pytest.mark.asyncio
async def test_late_unexpected_preparation_error_drains_siblings_and_preserves_completed_batch():
    release = asyncio.Event()
    cancelled = asyncio.Event()
    async def fetch(url):
        if url.endswith("bad"):
            await release.wait()
            raise RuntimeError("unexpected parser failure")
        if url.endswith("slow"):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return page(url)
    sources = [{"url": BASE + tail} for tail in ("fast", "bad", "slow")]
    async with aclosing(pipeline(sources, fetch, parallel=3)) as stream:
        staged = await asyncio.wait_for(anext(stream), 2)
        release.set()
        with pytest.raises(RuntimeError, match="unexpected parser failure"):
            await anext(stream)
    assert cancelled.is_set() and len(staged) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["connect", "timeout", "blocked"])
async def test_failed_source_is_reported_once_and_other_sources_continue(failure):
    import httpx
    from app.services.scrape_assistant_tools import SourceReadError
    errors = {
        "connect": httpx.ConnectTimeout(""),
        "timeout": TimeoutError(),
        "blocked": SourceReadError("robots access denied", access_issue="robots_unavailable"),
    }
    calls, events, batches = [], [], []
    async def fetch(url):
        calls.append(url)
        if url.endswith("bad"):
            raise errors[failure]
        return page(url)
    async def observe(**event):
        events.append(event)
    async with aclosing(pipeline(
        [{"url": BASE + tail} for tail in ("first", "bad", "last")],
        fetch, parallel=1, on_progress=observe,
    )) as stream:
        async for batch in stream:
            batches.extend(batch)
    assert calls == [BASE + tail for tail in ("first", "bad", "last")]
    assert len(batches) == 5  # Four good options plus one failure outcome.
    failures = [orch._surrey_preparation_failure(link) for link in batches
                if link.get("_surrey_prepare_error")]
    assert len(failures) == 1
    assert failures[0]["url"] == BASE + "bad"
    assert failures[0]["error"].startswith("prepare: ")
    assert len([e for e in events if e["kind"] == "course_prepare_error"]) == 1
    assert max(e["completed"] for e in events) == 3
    # This result cannot enter the extraction fallback or recovery sweep.
    details = orch._extraction_failure_details(failures[0]["error"], result=failures[0])
    assert details["reason"] == "source_preparation_failed"
    assert details["retryable"] is False
    assert not orch._queue_recovery_sweep_candidate(
        failures[0], counter="errors", links=[], url_keys=set(), details=details,
    )


@pytest.mark.asyncio
async def test_collector_preserves_failed_source_identity_and_order():
    async def fetch(url):
        if url.endswith("bad"):
            raise TimeoutError()
        return page(url)
    result = await facts.expand_links(
        [{"url": BASE + "bad"}, {"url": BASE + "good"}], 10, fetch, parallel=2,
    )
    assert result[0]["url"] == BASE + "bad"
    assert "_surrey_prepare_error" in result[0]
    assert all("/good?" in link["url"] for link in result[1:])


@pytest.mark.parametrize("staged, expected", [(0, "failed_degraded"), (50, "completed_with_warnings")])
def test_source_failures_cannot_be_reported_as_clean_completion(staged, expected):
    summary = {"preparation_failed": 1, "errors": 1, "staged": staged}
    guard = orch._preparation_failure_guard(summary)
    assert guard["status"] == expected
    assert "pending rows are preserved" in guard["message"]
    assert orch._preparation_failure_guard({"staged": 50}) is None
