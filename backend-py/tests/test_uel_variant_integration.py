"""UEL catalogue fanout, scoped extraction and independent review identities."""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from app.services.scraper import orchestrator
from app.services.scraper.extractors.uel_variants import parse_uel_variants
from app.services.scraper.pipelines import single_course
from app.services.scraper.url_identity import canonical_course_url_key


URL = "https://www.uel.ac.uk/postgraduate/courses/msc-mechanical-engineering"


def mechanical_page(*, placement_requirements: bool = True) -> str:
    """Reduced real UEL markup; retain actual award labels and published facts."""
    groups = []
    for label, key in (("MSc", "msc"), ("MSc with Placement Year", "msc-with-placement-year")):
        groups.append(f"""
          <div class="outer-div">
            <div class="tab-degree-type"><h3 class="degree-type">{label}</h3></div>
          </div>
          <div class="course-option-details"><div class="course-option-details__list">
            <div class="course-option-details__list-item">
              <h4 class="course-route">Mechanical Engineering MSc</h4>
              <span class="application-type">International Applicant</span>
              <span class="attendance-type">Full time,</span>
              <span class="attendance-type-yr">1/2 years</span>
              <span class="fee-type">£17220 per year. Year 2 Industrial Placement Fee - £3,500</span>
              <a data-degreetype="{key}" data-applicationtype="international-applicant"
                 data-attendance="full-time">Apply direct</a>
            </div>
          </div></div>
        """)
    modals = []
    for label, key in (("MSc", "msc"), ("MSc with Placement Year", "msc-with-placement-year")):
        if key != "msc" and not placement_requirements:
            continue
        modals.append(f"""
          <button aria-label="Full entry requirements for {label}"
                  data-modal-id="entry-{key}">Full entry requirements</button>
          <dialog id="entry-{key}" class="modal-entry">
            <h2>Entry Requirements</h2>
            <p>IELTS overall 6.0 with a minimum of 6.0 in Writing and Speaking;
               5.5 in Reading and Listening.</p>
          </dialog>
        """)
    return f"""<html><head><title>Mechanical Engineering MSc</title></head><body>
      <h1>Mechanical Engineering MSc</h1>
      <div class="course-options-content-div" id="September2026" aria-label="September 2026">
        {''.join(groups)}
      </div>{''.join(modals)}
      <p>Location: Docklands</p>
    </body></html>"""


@pytest.fixture(autouse=True)
def no_snapshot_io(monkeypatch):
    from app.services.scraper import http_fetcher

    monkeypatch.setattr(orchestrator, "_save_extraction_snapshot_safe", AsyncMock())
    # Individual tests opt in to static success; never make a paid request.
    monkeypatch.setattr(http_fetcher, "fetch_html_scrape_do", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_discovery_fanout_extracts_two_records_from_one_fetch(monkeypatch):
    from app.services.scraper import http_fetcher

    fetch = AsyncMock(return_value=mechanical_page())
    monkeypatch.setattr(http_fetcher, "fetch_html_scrape_do", fetch)
    monkeypatch.setattr(http_fetcher, "fetch_html", AsyncMock(side_effect=AssertionError("direct fetch")))
    # These must never run on a semantic route, even with a sparse page.
    monkeypatch.setattr(single_course, "fetch_html", AsyncMock(side_effect=AssertionError("refetch")))
    links = await orchestrator._expand_uel_course_links([
        {"url": URL, "name": "Mechanical Engineering MSc", "payload": {"ielts_overall": 9}},
        {"url": URL + "/", "name": "Alias"},
    ])
    assert len(links) == 2
    assert len({link["name"] for link in links}) == 2
    assert len({canonical_course_url_key(link["url"]) for link in links}) == 2
    assert all("uel_variant=" in link["url"] and "payload" not in link for link in links)
    fetch.assert_awaited_once()
    results = await asyncio.gather(*(
        orchestrator._extract_only(
            link, "United Kingdom",
            central_data={"english": {"ielts_overall": 9}},
            uni_pdf_data={"english": {"ielts_overall": 9}},
        ) for link in links
    ))
    assert all(not result.get("error") for result in results), results
    assert sorted(result["payload"]["duration"] for result in results) == [1, 2]
    assert all(result["payload"]["ielts_overall"] == 6 for result in results)
    assert all(result["payload"]["international_fee"] == 17220 for result in results)
    assert all(result["payload"]["course_website"] == result["url"] for result in results)
    assert all(result["_uel_variant"] for result in results)


@pytest.mark.asyncio
async def test_reextract_stored_url_selects_only_own_modal_and_never_defaults(monkeypatch):
    from app.services.scraper import http_fetcher

    html = mechanical_page(placement_requirements=False)
    variants = parse_uel_variants(html, URL)
    placement = next(v for v in variants if "placement" in v.key)
    fetch = AsyncMock(return_value=html)
    monkeypatch.setattr(http_fetcher, "fetch_html_scrape_do", fetch)
    result = await orchestrator._extract_only(
        {"url": placement.url, "name": placement.name},
        "United Kingdom",
        central_data={"english": {"ielts_overall": 9}},
        uni_pdf_data={"english": {"ielts_overall": 9}},
    )
    assert not result.get("error"), result
    assert result["payload"]["duration"] == 2
    assert result["payload"].get("ielts_overall") is None
    assert result["payload"]["course_name"] == placement.name
    assert result["payload"]["course_website"] == placement.url
    fetch.assert_awaited_once_with(URL, render=False)
    with pytest.raises(ValueError, match="no longer exists|not present"):
        await single_course.extract_course(URL + "?uel_variant=removed-award", html=html)
    with pytest.raises(ValueError, match="multiple course routes"):
        await single_course.extract_course(URL, html=html)


@pytest.mark.asyncio
async def test_retry_resume_preserves_individual_variant_identity(monkeypatch):
    from app.services.scraper import http_fetcher

    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr(http_fetcher, "fetch_html", fetch)
    monkeypatch.setattr(http_fetcher, "fetch_html_scrape_do", AsyncMock(return_value=None))
    failed = await orchestrator._expand_uel_course_links([{"url": URL}])
    assert failed[0]["_uel_skip_preflight"] is True
    # A subsequent job retries discovery; no failed parent checkpoint exists.
    fetch.return_value = mechanical_page()
    links = await orchestrator._expand_uel_course_links([{"url": URL}])
    assert fetch.await_count == 2
    staged_key = orchestrator._normalize_course_url(links[0]["url"])
    # A legacy parent checkpoint does not satisfy either route.
    done = {staged_key, orchestrator._normalize_course_url(URL)}
    remaining = [link for link in links if orchestrator._normalize_course_url(link["url"]) not in done]
    assert remaining == [links[1]]
    # A targeted retry must not silently fan out to its already-approved sibling.
    fetch.side_effect = None
    fetch.return_value = mechanical_page()
    fetch.reset_mock()
    targeted = await orchestrator._expand_uel_course_links([remaining[0]])
    assert [link["url"] for link in targeted] == [remaining[0]["url"]]
    fetch.assert_not_awaited()
    # Expansion failures enter ordinary per-course recovery, not a hard error
    # sentinel. Prove the orchestrator forwards that instruction.
    recovered = {"url": URL, "payload": {"course_name": "Single route MSc"}}
    extraction = AsyncMock(return_value=recovered)
    monkeypatch.setattr(orchestrator, "extract_course", extraction)
    failed_result = await orchestrator._extract_only(failed[0], "United Kingdom")
    assert failed_result["payload"]["course_name"] == "Single route MSc"
    assert extraction.call_args.kwargs["_uel_skip_preflight"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["stop", "deadline", "account_error"])
async def test_expansion_cancels_and_drains_outstanding_fetches(monkeypatch, exit_kind):
    from app.services.scraper import uel_transport
    from app.services.scraper.http_fetcher import ScrapedoAccountError

    started = asyncio.Event()
    release = asyncio.Event()
    active = set()
    cancelled = []
    stop = [False]

    async def blocked_fetch(url):
        active.add(url)
        started.set()
        try:
            if exit_kind == "account_error" and url.endswith("-0"):
                await release.wait()
                raise ScrapedoAccountError("test account unavailable")
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(url)
            raise
        finally:
            active.discard(url)

    monkeypatch.setattr(uel_transport, "fetch_uel_source", blocked_fetch)
    task = asyncio.create_task(orchestrator._expand_uel_course_links(
        [{"url": URL + f"-{index}"} for index in range(8)],
        stop_flag=stop, phase_timeout=0.03 if exit_kind == "deadline" else 2,
    ))
    await started.wait()
    if exit_kind == "stop":
        stop[0] = True
        expected = asyncio.CancelledError
    elif exit_kind == "deadline":
        expected = TimeoutError
    else:
        release.set()
        expected = ScrapedoAccountError
    with pytest.raises(expected):
        await asyncio.wait_for(task, timeout=1)
    assert not active
    assert cancelled
    assert task.done()


@pytest.mark.asyncio
async def test_expansion_emits_progress_per_source_and_reuses_prefetched_html(monkeypatch):
    from app.services.scraper import uel_transport

    fetch = AsyncMock(return_value=mechanical_page())
    emit = AsyncMock()
    monkeypatch.setattr(uel_transport, "fetch_uel_source", fetch)
    links = await orchestrator._expand_uel_course_links(
        [{"url": URL}, {"url": URL + "-other"}], emit=emit,
    )
    progress = [
        call.kwargs for call in emit.await_args_list
        if call.kwargs.get("kind") == "uel_variant_expansion_progress"
    ]
    assert [event["completed"] for event in progress] == [1, 2]
    assert all(event["total"] == 2 for event in progress)
    assert len(links) == 4
    fetch.reset_mock()
    again = await orchestrator._expand_uel_course_links([
        {"url": URL, "_uel_html": mechanical_page()},
    ])
    assert len(again) == 2
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_selected_route_survives_removal_of_its_sibling(monkeypatch):
    from bs4 import BeautifulSoup
    from app.services.scraper import uel_transport

    page = BeautifulSoup(mechanical_page(), "html.parser")
    details = page.select(".course-option-details")[1]
    details.find_previous_sibling().decompose()
    details.decompose()
    for node in page.select(
        '[data-modal-id="entry-msc-with-placement-year"], #entry-msc-with-placement-year'
    ):
        node.decompose()
    html = str(page)
    assert parse_uel_variants(html, URL) == []
    result = await single_course.extract_course(URL + "?uel_variant=msc", html=html)
    assert result["payload"]["course_name"] == "Mechanical Engineering MSc"
    fetch = AsyncMock(return_value=html)
    monkeypatch.setattr(uel_transport, "fetch_uel_source", fetch)
    # Combining a base discovery link and selected retry must parse that retry
    # with its selector; it remains valid even when the source has one award.
    links = await orchestrator._expand_uel_course_links([
        {"url": URL}, {"url": URL + "?uel_variant=msc"},
    ])
    selected = next(link for link in links if "uel_variant=" in link["url"])
    assert not selected.get("_uel_expansion_error")
    assert selected["name"] == "Mechanical Engineering MSc"
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_variant_omissions_explicitly_clear_review_fields():
    from app.services.scraper.extractors.uel_variants import UELVariant

    variant = UELVariant(
        "msc", "Mechanical Engineering MSc", URL + "?uel_variant=msc",
        "<html><h1>Mechanical Engineering MSc</h1></html>", True, True,
    )
    result = await single_course._extract_uel_variant(variant, country="United Kingdom")
    for field in (
        "international_fee", "duration", "duration_term", "course_location",
        "intake_months", "intake_days", "ielts_overall", "academic_score",
    ):
        assert field in result["payload"]
        assert result["payload"][field] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [None, "<html>403 Forbidden</html>", "<html>course-options-content-div"])
async def test_uel_transport_uses_direct_alternate_after_incomplete_static(monkeypatch, first):
    from app.services.scraper import http_fetcher
    from app.services.scraper.uel_transport import fetch_uel_source

    order = []

    async def static_response(*args, **kwargs):
        order.append("static")
        return first

    async def direct_response(*args, **kwargs):
        order.append("direct")
        return mechanical_page()

    direct = AsyncMock(side_effect=direct_response)
    static = AsyncMock(side_effect=static_response)
    monkeypatch.setattr(http_fetcher, "fetch_html", direct)
    monkeypatch.setattr(http_fetcher, "fetch_html_scrape_do", static)
    source = await fetch_uel_source(URL + "?uel_variant=msc")
    assert source == mechanical_page()
    direct.assert_awaited_once_with(URL, retries=0)
    static.assert_awaited_once_with(URL, render=False)
    assert order == ["static", "direct"]


@pytest.mark.asyncio
async def test_uel_static_success_never_attempts_known_blocked_direct_transport(monkeypatch):
    from app.services.scraper import http_fetcher
    from app.services.scraper.uel_transport import fetch_uel_source

    static = AsyncMock(return_value=mechanical_page())
    direct = AsyncMock(side_effect=AssertionError("direct must not run"))
    monkeypatch.setattr(http_fetcher, "fetch_html_scrape_do", static)
    monkeypatch.setattr(http_fetcher, "fetch_html", direct)
    assert await fetch_uel_source(URL) == mechanical_page()
    static.assert_awaited_once_with(URL, render=False)
    direct.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cap, expected_budget", [(5, 180), (100, 800), (300, 1800)])
async def test_run_expansion_supplies_source_cap_and_scaled_budget(monkeypatch, cap, expected_budget):
    links = [{"url": URL + f"-{index}"} for index in range(300)]
    expansion = AsyncMock(return_value=[])
    monkeypatch.setattr(orchestrator, "_expand_uel_course_links", expansion)
    stop = [False]
    await orchestrator._expand_uel_course_links_for_run(
        links, max_courses=cap, targeted_retry=False, stop_flag=stop,
    )
    assert expansion.await_args.args[0] == links[:cap]
    assert expansion.await_args.kwargs["phase_timeout"] == expected_budget
    assert expansion.await_args.kwargs["stop_flag"] is stop


@pytest.mark.asyncio
async def test_selected_raw_snapshots_replay_without_losing_route_identity(monkeypatch):
    from app.services.scraper.snapshot_context import (
        consume_pending_snapshot, replay_mode_scope, snapshot_job_scope,
    )

    html = mechanical_page()
    variants = parse_uel_variants(html, URL)
    monkeypatch.setattr(single_course, "fetch_html", AsyncMock(side_effect=AssertionError("refetch")))
    snapshots = []
    with snapshot_job_scope(69, "uel_variant_snapshot_test"):
        for variant in variants:
            await single_course.extract_course(variant.url, html=html, country="United Kingdom")
            snapshot = consume_pending_snapshot()
            assert snapshot["url"] == variant.url
            assert snapshot["content"] == html
            snapshots.append(snapshot)
    with replay_mode_scope():
        for snapshot, variant in zip(snapshots, variants):
            replayed = await single_course.extract_course(
                snapshot["url"], html=snapshot["content"], country="United Kingdom",
            )
            assert replayed["payload"]["course_name"] == variant.name
            assert replayed["payload"]["course_website"] == variant.url
            assert consume_pending_snapshot() is None


@pytest.mark.asyncio
async def test_unselected_uel_keeps_original_pipeline_when_preflight_is_unavailable(monkeypatch):
    from app.services.scraper import uel_transport
    from app.services.scraper.config import context

    monkeypatch.setattr(uel_transport, "fetch_uel_source", AsyncMock(
        side_effect=ValueError("UEL source fetch failed"),
    ))
    # The ordinary pipeline starts by loading its university context. Reaching
    # this sentinel proves the source preflight did not short-circuit it.
    def original_pipeline():
        raise RuntimeError("original pipeline reached")

    monkeypatch.setattr(context, "require_uni_config", original_pipeline)
    with pytest.raises(RuntimeError, match="original pipeline reached"):
        await single_course.extract_course(URL)
    with pytest.raises(ValueError, match="UEL source fetch failed"):
        await single_course.extract_course(URL + "?uel_variant=msc")


@pytest.mark.asyncio
async def test_normal_recovery_cannot_stage_a_recovered_multi_route_parent(monkeypatch):
    from app.services.scraper import uel_transport
    from app.services.scraper.config.context import set_uni_config
    from app.services.scraper.config.schema import UniConfig

    set_uni_config(UniConfig(
        slug="uel", name="University of East London",
        base_url="https://www.uel.ac.uk", scrape_url=URL,
    ))

    monkeypatch.setattr(uel_transport, "fetch_uel_source", AsyncMock(
        side_effect=AssertionError("preflight must not repeat"),
    ))
    recovered_fetch = AsyncMock(return_value=mechanical_page())
    monkeypatch.setattr(single_course, "fetch_html", recovered_fetch)
    with pytest.raises(ValueError, match="recovered page has multiple course routes"):
        await single_course.extract_course(
            URL, country="United Kingdom", use_ai_fallback=False,
            _uel_skip_preflight=True,
        )
    recovered_fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_domestic_routes_are_rejected_by_existing_global_gate():
    from app.services.scraper.guards import should_stage_course

    html = mechanical_page().replace("International Applicant", "Home Applicant").replace(
        'data-applicationtype="international-applicant"', 'data-applicationtype="home-applicant"'
    )
    variants = parse_uel_variants(html, URL)
    assert len(variants) == 2
    for variant in variants:
        result = await single_course.extract_course(variant.url, html=html)
        assert result["payload"]["domestic_only"] is True
        assert result["payload"].get("international_fee") is None
        assert should_stage_course(
            variant.name, result["payload"], source_url=variant.url
        ) == (False, "domestic_only")


@pytest.mark.asyncio
async def test_real_staging_and_approval_do_not_collapse_variants(monkeypatch):
    """Real DB integration, isolated in a disposable university."""
    from app.database import AsyncSessionLocal, engine
    from app.models import AcademicRequirement, Course, Fee, Intake, ScrapedCourse, University
    from app.services.scraper.approve_course import approve_scraped_course
    from app.services.scraper.stage_course import stage_course
    from app.services.scraper import http_fetcher

    await engine.dispose()
    marker = uuid.uuid4().hex[:12]
    university_id = None
    monkeypatch.setattr(http_fetcher, "fetch_html", AsyncMock(return_value=mechanical_page()))
    try:
        async with AsyncSessionLocal() as db:
            university = University(
                name=f"UEL integration {marker}", country="United Kingdom",
                city="London", website="https://www.uel.ac.uk",
            )
            db.add(university)
            await db.commit()
            await db.refresh(university)
            university_id = university.id
            legacy = Course(
                university_id=university_id,
                name="  Mechanical   Engineering MSc  ",
                course_website=URL.replace("https://www.", "http://") + "/",
            )
            db.add(legacy)
            wrong_source = Course(
                university_id=university_id, name="Mechanical Engineering MSc",
                course_website=URL + "-unrelated",
            )
            wrong_award = Course(
                university_id=university_id, name="Mechanical Engineering MFA",
                course_website=URL,
            )
            db.add_all([wrong_source, wrong_award])
            await db.commit()
            await db.refresh(legacy)
            legacy_id = legacy.id
            links = await orchestrator._expand_uel_course_links([{"url": URL}])
            rows = []
            for link in links:
                result = await orchestrator._extract_only(link, "United Kingdom")
                assert not result.get("error"), result
                staged = await stage_course(
                    db, scrape_job_id=f"uel_variant_{marker}",
                    university_id=university_id, course_name=result["name"],
                    source_url=result["url"], payload=result["payload"], evidence=result["evidence"],
                )
                assert staged.saved, staged
                rows.append(await db.get(ScrapedCourse, staged.scraped_course_id))
            assert rows[0].id != rows[1].id
            assert rows[0].canonical_course_url != rows[1].canonical_course_url
            assert all(row.international_fee == 17220 for row in rows)
            assert sorted(row.duration for row in rows) == [1, 2]
            approved = [await approve_scraped_course(db, rows[0])]
            assert approved[0]["course_id"] == legacy_id
            assert rows[1].status != "approved"
            approved.append(await approve_scraped_course(db, rows[1]))
            assert approved[0]["course_id"] != approved[1]["course_id"]
            # Rename one route to the sibling's name; semantic identity wins
            # over approval's legacy name-only lookup.
            rows[0].course_name = rows[1].course_name
            repeated = await approve_scraped_course(db, rows[0])
            assert repeated["course_id"] == approved[0]["course_id"]
            courses = (await db.execute(
                select(Course).where(Course.university_id == university_id)
            )).scalars().all()
            assert len(courses) == 4
            assert sum("uel_variant=" in (course.course_website or "") for course in courses) == 2
            assert wrong_source.course_website == URL + "-unrelated"
            assert wrong_award.course_website == URL
            assert wrong_award.name == "Mechanical Engineering MFA"
            # A same-job retry is a duplicate only of its own route.
            duplicate = await stage_course(
                db, scrape_job_id=f"uel_variant_{marker}", university_id=university_id,
                course_name=result["name"], source_url=result["url"],
                payload=result["payload"], evidence=result["evidence"],
            )
            assert not duplicate.saved
            assert "duplicate_url_in_job" in duplicate.reason
            # A later scrape with no route-owned modal must not inherit an
            # English score from either approved sibling or its previous row.
            placement = next(
                variant for variant in parse_uel_variants(mechanical_page(), URL)
                if "placement" in variant.key
            )
            fresh = await single_course.extract_course(
                placement.url, html=mechanical_page(placement_requirements=False),
                country="United Kingdom",
            )
            restaged = await stage_course(
                db, scrape_job_id=f"uel_variant_{marker}_next",
                university_id=university_id, course_name=placement.name,
                source_url=placement.url, payload=fresh["payload"], evidence=fresh["evidence"],
            )
            assert restaged.saved
            fresh_row = await db.get(ScrapedCourse, restaged.scraped_course_id)
            assert fresh_row.ielts_overall is None
            assert all(row.status == "approved" for row in rows)
            # Approval must clear historical satellites when this route's
            # current source has no value, rather than retaining an old fee,
            # intake or academic requirement behind a null staged row.
            course_id = approved[1]["course_id"]
            db.add(AcademicRequirement(
                course_id=course_id, academic_level="Bachelor",
            ))
            await db.commit()
            from app.routers.scrape import ReExtractBody, re_extract_staged
            from app.services.scraper.config import loader
            from app.services.scraper.extractors.uel_variants import UELVariant

            omitted = await single_course._extract_uel_variant(
                UELVariant(
                    placement.key, placement.name, placement.url,
                    f"<html><h1>{placement.name}</h1></html>", True, True,
                ),
                country="United Kingdom",
            )
            omitted["payload"]["academic_level"] = None
            monkeypatch.setattr(loader, "get_config_for_host", lambda *a, **kw: None)
            monkeypatch.setattr(orchestrator, "_extract_only", AsyncMock(return_value=omitted))
            fixed = await re_extract_staged(
                ReExtractBody(ids=[fresh_row.id], university_id=university_id), db,
            )
            assert fixed["updated"] == 1, fixed
            await db.refresh(fresh_row)
            for field in ("international_fee", "duration", "course_location", "intake_months"):
                assert getattr(fresh_row, field) is None
            cleared = await approve_scraped_course(db, fresh_row)
            assert cleared["course_id"] == course_id
            for model in (Fee, Intake, AcademicRequirement):
                assert (await db.execute(
                    select(model).where(model.course_id == course_id)
                )).scalars().all() == []
            course = await db.get(Course, course_id)
            assert course.duration is None
            assert course.course_location is None
    finally:
        if university_id is not None:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(University).where(University.id == university_id))
                await db.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_ambiguous_legacy_parent_requires_review():
    from app.services.scraper.approve_course import approve_scraped_course

    candidates = [
        SimpleNamespace(name="Mechanical Engineering MSc", course_website=URL),
        SimpleNamespace(name="mechanical engineering msc", course_website=URL + "/"),
    ]
    db = SimpleNamespace(execute=AsyncMock(side_effect=[
        None, SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: candidates)),
    ]))
    sc = SimpleNamespace(
        id=1, university_id=69, course_name="Mechanical Engineering MSc",
        course_website=URL + "?uel_variant=msc",
    )
    with pytest.raises(ValueError, match="Ambiguous legacy UEL"):
        await approve_scraped_course(db, sc)