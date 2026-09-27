"""Qualification-aware ULaw publication against a captured official course page.

Source: https://www.law.ac.uk/study/postgraduate/law/pg-dip-and-pg-cert-legal-technology/
Captured: 2026-09-27 UTC, /tmp/legal-technology-current.html.
Full response SHA-256: 629fe3d1bc8b61809376bb5ecefca3618bc0db5eeb86c5908dbe476ffe32edac.
The compact fixture retains the exact published headings, locations, course
introduction, start-date entries and fee paragraphs; surrounding presentational
markup is omitted. Its SHA-256 is pinned below to catch accidental price edits.
"""
from copy import deepcopy
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import Course, CourseOffering, Fee, FieldConflict, Intake, ScrapedCourse, ScrapedFieldEvidence, University
from app.models.page_snapshot import PageSnapshot
from app.models.scrape_runtime import ScrapeRuntimeJob
from app.routers import staged_selected_approval as route
from app.services.scraper.extractors import ulaw_fees
from app.services.scraper.fee_selection import fee_selection
from app.services.scraper.snapshot_save import persist_staged_row_backup
from app.services.scraper.ulaw_qualifications import QUALIFICATION_SCOPE
from tests.test_campus_fee_split import migrate_offerings_in_transaction

URL = "https://www.law.ac.uk/study/postgraduate/law/pg-dip-and-pg-cert-legal-technology/"
HTML = """<link rel="canonical" href="https://www.law.ac.uk/study/postgraduate/law/pg-dip-and-pg-cert-legal-technology/" />
<h4>PG Dip and PG Cert</h4>
<h1 class="mb-3">Legal Technology</h1>
<div id="course-details-header-intro">
<p>The Postgraduate Diploma and Postgraduate Certificate in Legal Technology are innovative, flexible programmes designed for law and non-law graduates who would like to gain essential in-depth knowledge of Legal Technology without completing a full Master&rsquo;s programme.</p>
<p>If you would prefer to study the full Master's programme, we have an <a href="/study/postgraduate/law/msc-legal-technology/">Master of Sciences (MSc) in Legal Technology</a> available.</p>
</div>
<section class="key-facts key-facts--undergraduate my-medium pb-medium">
<div class="key-facts__locations col-lg-3 my-4 my-lg-0">
<h4>Locations </h4>
<a href="/locations/bristol/" >Bristol</a>, <a href="/locations/london/moorgate/" >London Moorgate</a> and <a href="/locations/online/" >Online</a>
</div></section>
<h3>PG Dip and PG Cert Legal Technology</h3>
<div id="accordion-bcz" class="accordion accordion--course-details">
<div class="accordion__item">
<div class="accordion__header collapsed" data-toggle="collapse" data-target="#collapse-bcz1" aria-expanded="false" aria-controls="collapse-bcz1" id="heading-bcz1">
<h4>October 2026</h4>
</div>
<div id="collapse-bcz1" class="collapse" aria-labelledby="heading-bcz1" data-parent="#accordion-bcz" style="">
<div class="accordion__body"><ul>
<li><strong>PG Cert Legal Technology (Postgraduate Certificate) </strong><ul><li>Part-time: London Moorgate</li></ul></li>
<li><strong>PG Dip Legal Technology (Postgraduate Diploma) </strong><ul><li>Full-time: London Moorgate</li><li>Part-time: London Moorgate</li></ul></li>
</ul></div></div></div>
<div class="accordion__item">
<div class="accordion__header collapsed" data-toggle="collapse" data-target="#collapse-bcz2" aria-expanded="false" aria-controls="collapse-bcz2" id="heading-bcz2">
<h4>February 2027</h4>
</div>
<div id="collapse-bcz2" class="collapse" aria-labelledby="heading-bcz2" data-parent="#accordion-bcz" style="">
<div class="accordion__body"><ul><li><strong>PG Dip Legal Technology (Postgraduate Diploma) </strong><ul>
<li>Full-time: Bristol and London Moorgate</li>
<li>Part-time: Bristol and London Moorgate</li>
</ul></li></ul></div></div></div>
<div class="accordion__item">
<div class="accordion__header collapsed" data-toggle="collapse" data-target="#collapse-bcz3" aria-expanded="false" aria-controls="collapse-bcz3" id="heading-bcz3">
<h4>October 2027</h4>
</div>
<div id="collapse-bcz3" class="collapse" aria-labelledby="heading-bcz3" data-parent="#accordion-bcz" style="">
<div class="accordion__body"><ul>
<li><strong>PG Cert Legal Technology (Postgraduate Certificate) </strong><ul><li>Full-time: Bristol and London Moorgate</li><li>Part-time: Bristol and London Moorgate</li></ul></li>
<li><strong>PG Dip Legal Technology (Postgraduate Diploma) </strong><ul><li>Full-time: Bristol and London Moorgate</li><li>Part-time: Bristol and London Moorgate</li></ul></li>
</ul></div></div></div>
</div>
<a class="nav-link h4 " href="#cxx2677" role="tab" data-toggle="tab" aria-selected="false">Fees and Funding</a>
<div role="tabpanel" class="tab-pane fade  show" id="cxx2677">
<p><span style="text-decoration: underline;"><strong>2026/27 Course Fees (for courses starting between 1 July 2026 - 31 May 2027)</strong></span></p>
<p><strong>PG Dip Domestic students<br /></strong>London: &pound;10,000<br />Outside of London: &pound;8,550</p>
<p><strong>PG Dip Non-domestic students<br /></strong>London: &pound;13,150<br />Outside of London: &pound;12,200</p>
<p><strong>PG Cert Domestic students<br /></strong>London: &pound;5,100<br />Outside of London: &pound;4,250</p>
<p><strong>PG Cert Non-domestic students<br /></strong>London: &pound;6,600<br />Outside of London: &pound;6,150</p>
<p><span style="text-decoration: underline;"><strong>2027/28 Course Fees (for courses starting between 1 June 2027 - 31 May 2028)</strong></span></p>
<p><strong>PG Dip Domestic students<br /></strong>London: &pound;10,250<br />Outside of London: &pound;8,750</p>
<p><strong>PG Dip Non-domestic students<br /></strong>London: &pound;13,500<br />Outside of London: &pound;12,500</p>
<p><strong>PG Cert Domestic students<br /></strong>London: &pound;5,250<br />Outside of London: &pound;4,350</p>
<p><strong>PG Cert Non-domestic students<br /></strong>London: &pound;6,750<br />Outside of London: &pound;6,300</p>
</div>"""


def freeze_cohort(monkeypatch):
    """Freeze the parser's current cohort without changing its selection algorithm."""
    class CaptureDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 27)

    monkeypatch.setattr(ulaw_fees, "date", CaptureDate)


def mock_official_fetch(monkeypatch, html=HTML, status=200):
    real = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text=html, request=request))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=transport, **kwargs))


def counted_official_fetch(monkeypatch, html=HTML):
    """Mutable upstream, not mutable proof: every GET records its exact route."""
    state = {"html": html, "status": 200, "calls": []}
    real = httpx.AsyncClient

    def respond(request):
        state["calls"].append((request.method, str(request.url)))
        return httpx.Response(state["status"], text=state["html"], request=request)

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real(transport=transport, **kwargs))
    return state


async def prepare_qualification_children(db, row_id):
    from app.services.scraper.ulaw_qualifications import split_pending_qualifications
    from app.services.scraper.campus_fee_split import split_pending_course
    row = await db.get(ScrapedCourse, row_id)
    awards = await split_pending_qualifications(db, row)
    assert awards["status"] == "split"
    ids = []
    for award_id in awards["courseIds"]:
        split = await split_pending_course(db, await db.get(ScrapedCourse, award_id))
        ids.extend(split["courseIds"])
    await db.commit()
    return ids


@pytest_asyncio.fixture
async def db():
    from app.database import engine as configured_engine
    engine = create_async_engine(configured_engine.url, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await migrate_offerings_in_transaction(connection)
        async with AsyncSession(bind=connection, expire_on_commit=False,
                                join_transaction_mode="create_savepoint") as session:
            yield session
        await transaction.rollback()
    await engine.dispose()


def captured_values():
    authority = ulaw_fees.parse_course_fees(HTML, URL, today=date(2026, 9, 27))
    assert authority and authority["fee_year"] == 2026
    assert {(o["study_variant"], o["campus"], o["amount"]) for o in authority["selected"]} == {
        ("PG Dip", "London", 13150), ("PG Dip", "Outside of London", 12200),
        ("PG Cert", "London", 6600), ("PG Cert", "Outside of London", 6150),
    }
    return {
        "course_name": "Legal Technology", "course_website": URL,
        "course_location": "Bristol, London Moorgate, Online",
        "international_fee": None, "currency": "GBP", "fee_year": 2026,
        "fee_term": "Full Course", "extraction_method": {
            "international_fee": ulaw_fees.METHOD, "fee_variants": authority,
        },
        "scrape_warnings": ["international_fee_varies_by_campus"],
        "status": "pending", "duration": 1, "duration_term": "Year",
        "ielts_overall": 6, "intake_months": ["October", "February"], "study_mode": "On Campus",
        "degree_level": "Postgraduate",
    }


async def seed(db, values=None):
    uni = University(name=f"ULaw qualification test {uuid4()}", country="United Kingdom", city="London")
    db.add(uni)
    await db.flush()
    job_id = str(uuid4())
    db.add(ScrapeRuntimeJob(runtime_job_id=job_id, university_id=uni.id,
                           job_type="scrape", status="completed"))
    await db.flush()
    row = ScrapedCourse(university_id=uni.id, scrape_job_id=job_id,
                        **(values if values is not None else captured_values()))
    db.add(row)
    await db.flush()
    evidence = ScrapedFieldEvidence(scraped_course_id=row.id, field_key="international_fee",
                                    source_url=URL, snippet="Published qualification-specific non-domestic fees",
                                    extraction_method=ulaw_fees.METHOD)
    db.add(evidence)
    await db.flush()
    db.add(FieldConflict(scraped_course_id=row.id, field_key="international_fee",
                         value_a="13150", value_b="6600", evidence_a_id=evidence.id,
                         conflict_type="qualification", reason="Historic award ambiguity"))
    await persist_staged_row_backup(db, row)
    await db.commit()
    return uni.id, job_id, row.id


async def published(db, uni):
    courses = (await db.execute(select(Course).where(Course.university_id == uni))).scalars().all()
    offerings = (await db.execute(select(CourseOffering).where(
        CourseOffering.course_id.in_([c.id for c in courses])
    ))).scalars().all() if courses else []
    return courses, offerings


def test_official_qualification_fixture_and_parser(monkeypatch):
    freeze_cohort(monkeypatch)
    assert sha256(HTML.encode()).hexdigest() == "6261211a7845a0a71f26e32ca5584c8ceb378c8101f94eb22092d424944ae19a"
    authority = captured_values()["extraction_method"]["fee_variants"]
    assert all(o["source_url"] == URL and o["period"] == "Full Course" for o in authority["selected"])
    assert "PG Dip and PG Cert Legal Technology" in HTML and "London Moorgate" in HTML
    # Published outside-London Certificate prices are fee evidence, not proof
    # that a 2026/27 Certificate is offered in Bristol.
    assert next(o for o in authority["selected"] if o["study_variant"] == "PG Cert"
                and o["campus"] == "Outside of London")["amount"] == 6150
    assert "PG Cert Legal Technology (Postgraduate Certificate) </strong><ul><li>Part-time: London Moorgate" in HTML
    assert "October 2027" in HTML and "Full-time: Bristol and London Moorgate" in HTML


@pytest.mark.asyncio
async def test_approve_selected_publishes_separate_awards_and_physical_offerings(db, monkeypatch):
    freeze_cohort(monkeypatch)
    mock_official_fetch(monkeypatch)
    uni, job, original_id = await seed(db)
    initial_backups = (await db.execute(select(PageSnapshot).where(
        PageSnapshot.scrape_job_id == job, PageSnapshot.snapshot_type == "staged_row"
    ))).scalars().all()
    assert initial_backups and any(s.original_extraction["course_name"] == "Legal Technology"
                                   for s in initial_backups)

    result = await route.approve_selected(route.ApproveSelectedBody(courseIds=[original_id]), db,
                                          {"email": "test-reviewer"})
    assert result["failed"] == [] and original_id in result["approvedIds"], result
    courses, offerings = await published(db, uni)
    assert len(courses) == 2
    diploma = next(c for c in courses if "dip" in c.name.lower())
    certificate = next(c for c in courses if "cert" in c.name.lower())
    assert diploma.id != certificate.id
    assert (diploma.name, diploma.degree_level) == ("PG Dip Legal Technology", "Graduate Diploma")
    assert (certificate.name, certificate.degree_level) == ("PG Cert Legal Technology", "Graduate Certificate")
    intakes = (await db.execute(select(Intake).where(
        Intake.course_id.in_([diploma.id, certificate.id])
    ))).scalars().all()
    assert {(i.course_id, i.intake_month) for i in intakes} == {
        (diploma.id, "October"), (diploma.id, "February"),
        (certificate.id, "October"),
    }
    assert len(intakes) == 3
    assert len(offerings) == 3
    assert {(c.name, o.location, o.fee_amount, o.fee_year, o.fee_currency, o.fee_term, o.source_url)
            for c in courses for o in offerings if o.course_id == c.id} == {
        ("PG Dip Legal Technology", "Bristol", 12200, 2026, "GBP", "Full Course", URL),
        ("PG Dip Legal Technology", "London Moorgate", 13150, 2026, "GBP", "Full Course", URL),
        ("PG Cert Legal Technology", "London Moorgate", 6600, 2026, "GBP", "Full Course", URL),
    }
    assert {o.location: o.fee_amount for o in offerings if o.course_id == diploma.id} == {
        "Bristol": 12200, "London Moorgate": 13150,
    }
    assert {o.location: o.fee_amount for o in offerings if o.course_id == certificate.id} == {
        "London Moorgate": 6600,
    }
    assert all(o.fee_year == 2026 and o.fee_currency == "GBP" and o.fee_term == "Full Course"
               and o.source_url == URL for o in offerings)
    assert not (await db.execute(select(Fee).where(Fee.course_id.in_([c.id for c in courses])))).scalars().all()
    source = await db.get(ScrapedCourse, original_id)
    assert source.status == "approved" and source.course_id in {diploma.id, certificate.id}
    evidence = (await db.execute(select(ScrapedFieldEvidence).where(
        ScrapedFieldEvidence.scraped_course_id.in_(result["approvedIds"])
    ))).scalars().all()
    assert {e.scraped_course_id for e in evidence} == set(result["approvedIds"])
    assert all(e.source_url == URL for e in evidence)
    conflicts = (await db.execute(select(FieldConflict).where(
        FieldConflict.scraped_course_id.in_(result["approvedIds"])
    ))).scalars().all()
    assert {c.scraped_course_id for c in conflicts} == set(result["approvedIds"])
    assert all(c.reason == "Historic award ambiguity" for c in conflicts)
    backups = (await db.execute(select(PageSnapshot).where(
        PageSnapshot.scrape_job_id == job, PageSnapshot.snapshot_type == "staged_row"
    ))).scalars().all()
    assert len(backups) >= len(initial_backups)
    from app.services.scraper.campus_fee_split import scope_refresh_payload
    for staged_id in result["approvedIds"]:
        staged = await db.get(ScrapedCourse, staged_id)
        award = staged.extraction_method[QUALIFICATION_SCOPE]["award"]
        assert staged.intake_months == (["October", "February"] if award == "PG Dip" else ["October"])
        assert all(o["study_variant"] == award
                   for o in staged.extraction_method["fee_variants"]["selected"])
        award_locations = ["Bristol", "London Moorgate"] if award == "PG Dip" else ["London Moorgate"]
        assert staged.extraction_method["campus_authority"]["locations"] == award_locations
        assert staged.course_location in award_locations
        assert (award != "PG Cert" or staged.course_location == "London Moorgate")
        refresh = scope_refresh_payload(staged, {
            **captured_values(), "ielts_overall": 7,
        })
        assert "course_name" not in refresh and "course_location" not in refresh
        assert refresh["extraction_method"]["ulaw_qualification_scope"] == staged.extraction_method["ulaw_qualification_scope"]
        assert refresh["international_fee"] == staged.international_fee
        assert refresh["intake_months"] == staged.intake_months
        assert refresh["ielts_overall"] == 7
        assert all(o["study_variant"] == award
                   for o in refresh["extraction_method"]["fee_variants"]["selected"])
        assert refresh["extraction_method"]["campus_authority"]["locations"] == award_locations
    old_ids = {o.id for o in offerings}
    retry = await route.approve_selected(route.ApproveSelectedBody(courseIds=[original_id]), db,
                                         {"email": "test-reviewer"})
    assert retry["failed"] == [] and retry["splitCount"] == 0
    assert {c.id for c in (await published(db, uni))[0]} == {diploma.id, certificate.id}
    assert {o.id for o in (await published(db, uni))[1]} == old_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_2026_cert", "mislabelled_dip", "no_intake_table",
                                    "only_2027_intake", "cert_extra_february"])
async def test_award_specific_intake_proof_cannot_fall_back_to_shared_locations(db, monkeypatch, damage):
    freeze_cohort(monkeypatch)
    values = captured_values()
    uni, job, original_id = await seed(db, values)
    if damage == "missing_2026_cert":
        html = HTML.replace(
            '<li><strong>PG Cert Legal Technology (Postgraduate Certificate) </strong><ul><li>Part-time: London Moorgate</li></ul></li>',
            "",
        )
    elif damage == "mislabelled_dip":
        html = HTML.replace(
            '<strong>PG Dip Legal Technology (Postgraduate Diploma) </strong><ul>\n'
            '<li>Full-time: Bristol and London Moorgate</li>',
            '<strong>PG Cert Legal Technology (Postgraduate Certificate) </strong><ul>\n'
            '<li>Full-time: Bristol and London Moorgate</li>',
        )
    elif damage == "no_intake_table":
        html = HTML[:HTML.index('<div id="accordion-bcz"')] + HTML[HTML.index('<a class="nav-link h4 "'):]
    elif damage == "cert_extra_february":
        february = HTML.index('<h4>February 2027</h4>')
        insert_at = HTML.index('</ul></div></div></div>', february)
        html = HTML[:insert_at] + (
            '<li><strong>PG Cert Legal Technology (Postgraduate Certificate) </strong>'
            '<ul><li>Part-time: London Moorgate</li></ul></li>'
        ) + HTML[insert_at:]
    else:
        start = HTML.index('<div id="accordion-bcz"')
        # The February heading is inside its item: retain only the October
        # 2027 block, leaving Key Facts and 2026 fees unchanged.
        october_2027 = HTML.rfind('<div class="accordion__item">', start, HTML.index('<h4>October 2027</h4>'))
        html = HTML[:start] + '<div id="accordion-bcz" class="accordion accordion--course-details">\n' + HTML[october_2027:]
    assert html != HTML and "2026/27 Course Fees" in html and "key-facts__locations" in html
    if damage != "no_intake_table":
        assert "October 2027" in html
    mock_official_fetch(monkeypatch, html)
    result = await route.approve_selected(route.ApproveSelectedBody(courseIds=[original_id], force=True),
                                          db, {"email": "test-reviewer"})
    assert result["approvedIds"] == [] and len(result["failed"]) == 1, result
    assert result["failed"][0]["error"]
    original = await db.get(ScrapedCourse, original_id)
    await db.refresh(original)
    assert original.status == "pending" and original.course_id is None
    assert original.extraction_method == values["extraction_method"]
    assert not (await published(db, uni))[0]
    assert not (await db.execute(select(ScrapedCourse).where(
        ScrapedCourse.university_id == uni, ScrapedCourse.scrape_job_id == job,
        ScrapedCourse.id != original_id
    ))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", [False, True])
async def test_selected_proof_one_get_snapshot_and_next_request_refetch(db, monkeypatch, prepared):
    freeze_cohort(monkeypatch)
    upstream = counted_official_fetch(monkeypatch)
    uni, _, row_id = await seed(db)
    ids = await prepare_qualification_children(db, row_id) if prepared else [row_id]
    upstream["calls"].clear()
    original = route.approve_scraped_course

    async def change_source_after_child(*args, **kwargs):
        result = await original(*args, **kwargs)
        upstream["html"] = "<h1>Source changed during transaction</h1>"
        return result

    monkeypatch.setattr(route, "approve_scraped_course", change_source_after_child)
    result = await route.approve_selected(route.ApproveSelectedBody(courseIds=ids), db, {"email": "reviewer"})
    assert not result["failed"], result
    assert len(result["approvedIds"]) == 3
    assert upstream["calls"] == [("GET", URL)]
    assert len((await published(db, uni))[0]) == 2
    # Historical retries do not consume even one fresh proof.
    retry = await route.approve_selected(route.ApproveSelectedBody(courseIds=result["approvedIds"]),
                                         db, {"email": "reviewer"})
    assert not retry["failed"] and upstream["calls"] == [("GET", URL)]
    other_uni, _, other_id = await seed(db)
    second = await route.approve_selected(route.ApproveSelectedBody(courseIds=[other_id]), db, {"email": "reviewer"})
    assert not second["approvedIds"] and second["failed"]
    assert len(upstream["calls"]) == 2
    assert not (await published(db, other_uni))[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["fetch", "after_child"])
async def test_selected_proof_rollback_retry_and_same_request_isolation(db, monkeypatch, failure):
    freeze_cohort(monkeypatch)
    upstream = counted_official_fetch(monkeypatch)
    uni, _, row_id = await seed(db)
    ids = await prepare_qualification_children(db, row_id)
    other_uni, _, other_id = await seed(db)
    upstream["calls"].clear()
    original = route.approve_scraped_course
    original_rollback = db.rollback
    failed_once = False

    async def fail_child(*args, **kwargs):
        nonlocal failed_once
        result = await original(*args, **kwargs)
        if failure == "after_child" and not failed_once:
            failed_once = True
            raise route.ApprovalValidationError("Deliberate transactional failure")
        return result

    async def restore_upstream():
        await original_rollback()
        upstream["status"] = 200

    monkeypatch.setattr(route, "approve_scraped_course", fail_child)
    monkeypatch.setattr(db, "rollback", restore_upstream)
    if failure == "fetch":
        upstream["status"] = 503
    result = await route.approve_selected(route.ApproveSelectedBody(courseIds=[ids[0], other_id]),
                                          db, {"email": "reviewer"})
    assert len(result["failed"]) == 1 and result["failed"][0]["id"] == ids[0]
    assert other_id in result["approvedIds"]
    assert len(upstream["calls"]) == 2
    assert not (await published(db, uni))[0]
    assert len((await published(db, other_uni))[0]) == 2
    retry = await route.approve_selected(route.ApproveSelectedBody(courseIds=[ids[0]]), db, {"email": "reviewer"})
    assert not retry["failed"] and len(retry["approvedIds"]) == 3
    assert len(upstream["calls"]) == 3


@pytest.mark.asyncio
async def test_selected_proof_rechecks_coordinated_child_metadata(db, monkeypatch):
    from app.services.scraper.ulaw_qualifications import _contract_hash, validate_qualification_scope
    freeze_rollover(monkeypatch)
    html = live_capture()
    upstream = counted_official_fetch(monkeypatch, html)
    values = captured_values()
    values["fee_year"] = 2027
    values["extraction_method"]["fee_variants"] = ulaw_fees.parse_course_fees(html, URL)
    uni, _, row_id = await seed(db, values)
    ids = await prepare_qualification_children(db, row_id)
    upstream["calls"].clear()
    original = route.approve_scraped_course
    mutated = False

    async def mutate_next_award(db, child, **kwargs):
        nonlocal mutated
        result = await original(db, child, **kwargs)
        if not mutated:
            mutated = True
            for child_id in ids:
                target = await db.get(ScrapedCourse, child_id)
                metadata = deepcopy(target.extraction_method)
                if metadata[QUALIFICATION_SCOPE]["award"] != "PG Cert":
                    continue
                proof = metadata["campus_authority"]
                extra = deepcopy(proof["start_dates"][0])
                extra["intake"] = "February 2028"
                proof["start_dates"].append(extra)
                proof["snippet"] = " | ".join(e["snippet"] for e in proof["start_dates"])
                source = metadata[QUALIFICATION_SCOPE]["verified_source"]
                source["proofs"]["PG Cert"] = deepcopy(proof)
                source["contract_sha256"] = _contract_hash(source["authority"], source["proofs"])
                target.intake_months = ["October", "February"]
                target.extraction_method = metadata
                assert validate_qualification_scope(target)
            await db.flush()
        return result

    monkeypatch.setattr(route, "approve_scraped_course", mutate_next_award)
    result = await route.approve_selected(route.ApproveSelectedBody(courseIds=[row_id]), db, {"email": "reviewer"})
    assert result["failed"] and not result["approvedIds"]
    assert result["failed"][0]["reasonCode"] == "changed_cohort"
    assert upstream["calls"] == [("GET", URL)]
    assert not (await published(db, uni))[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["commit", "rollback"])
async def test_explicit_proof_context_transaction_lifecycle_and_direct_verification(db, monkeypatch, end):
    from app.services.scraper.ulaw_qualifications import SelectedQualificationProof, verify_qualification_source
    freeze_cohort(monkeypatch)
    upstream = counted_official_fetch(monkeypatch)
    _, _, row_id = await seed(db)
    ids = await prepare_qualification_children(db, row_id)
    upstream["calls"].clear()
    child = await db.get(ScrapedCourse, ids[0])
    context = SelectedQualificationProof(db)
    try:
        assert await verify_qualification_source(child, proof_context=context, db=db)
        assert await verify_qualification_source(child, proof_context=context, db=db)
        assert len(upstream["calls"]) == 1
        await getattr(db, end)()
        assert context._closed and not context._snapshots
        child = await db.get(ScrapedCourse, ids[0])
        assert not await verify_qualification_source(child, proof_context=context, db=db)
        assert len(upstream["calls"]) == 1
        assert await verify_qualification_source(child)
        assert await verify_qualification_source(child)
        assert len(upstream["calls"]) == 3
    finally:
        context.close()


@pytest.mark.asyncio
async def test_proof_cohort_isolation_and_detached_snapshot(db, monkeypatch):
    from app.services.scraper.ulaw_qualifications import SelectedQualificationProof
    freeze_cohort(monkeypatch)
    upstream = counted_official_fetch(monkeypatch, live_capture())
    await seed(db)
    # Begin the caller-owned transaction before creating its explicit capability.
    await db.execute(select(ScrapedCourse.id).limit(1))
    context = SelectedQualificationProof(db)
    old = captured_values()["extraction_method"]["fee_variants"]
    try:
        first = await context.fetch(db, old)
        first["proofs"]["PG Cert"]["locations"].append("Forged")
        assert "Forged" not in (await context.fetch(db, old))["proofs"]["PG Cert"]["locations"]
        freeze_rollover(monkeypatch)
        new = ulaw_fees.parse_course_fees(live_capture(), URL)
        second = await context.fetch(db, new)
        assert second["authority"]["fee_year"] == 2027
        assert (await context.fetch(db, old))["authority"]["fee_year"] == 2026
        assert len(upstream["calls"]) == 2
    finally:
        context.close()


@pytest.mark.asyncio
async def test_selected_mixed_cohort_parents_get_independent_split_proofs(db, monkeypatch):
    freeze_cohort(monkeypatch)
    html = live_capture()
    upstream = counted_official_fetch(monkeypatch, html)
    old_uni, _, old_id = await seed(db)
    values = captured_values()
    values["fee_year"] = 2027
    values["extraction_method"]["fee_variants"] = ulaw_fees.parse_course_fees(
        html, URL, today=date(2027, 9, 27),
    )
    new_uni, _, new_id = await seed(db, values)
    original_commit = db.commit

    async def rollover_after_transaction():
        await original_commit()
        freeze_rollover(monkeypatch)

    monkeypatch.setattr(db, "commit", rollover_after_transaction)
    result = await route.approve_selected(
        route.ApproveSelectedBody(courseIds=[old_id, new_id]), db, {"email": "reviewer"},
    )
    assert not result["failed"] and result["approvedCount"] == 7
    assert result["splitCount"] == 2
    assert upstream["calls"] == [("GET", URL), ("GET", URL)]
    assert {o.fee_year for o in (await published(db, old_uni))[1]} == {2026}
    assert {o.fee_year for o in (await published(db, new_uni))[1]} == {2027}


@pytest.mark.asyncio
async def test_direct_approval_without_capability_fetches_for_every_pending_child(db, monkeypatch):
    from app.services.scraper.approve_course import approve_scraped_course
    freeze_cohort(monkeypatch)
    upstream = counted_official_fetch(monkeypatch)
    _, _, row_id = await seed(db)
    ids = await prepare_qualification_children(db, row_id)
    upstream["calls"].clear()
    children = [await db.get(ScrapedCourse, child_id) for child_id in ids]
    for child in children:
        cohort = [member for member in children
                  if member.extraction_method[QUALIFICATION_SCOPE]["award"]
                  == child.extraction_method[QUALIFICATION_SCOPE]["award"]]
        await approve_scraped_course(db, child, actor="reviewer", commit=False, offering_cohort=cohort)
    assert upstream["calls"] == [("GET", URL)] * len(ids)
    await db.rollback()


def live_capture():
    path = Path(__file__).parent / "fixtures/ulaw_legal_technology_20260927.html"
    provenance = json.loads(path.with_suffix(".provenance.json").read_text())
    assert sha256(path.read_bytes()).hexdigest() == provenance["response_sha256"]
    assert provenance["source_url"] == URL
    return path.read_text()


def freeze_rollover(monkeypatch):
    class RolloverDate(date):
        @classmethod
        def today(cls):
            return cls(2027, 9, 27)
    monkeypatch.setattr(ulaw_fees, "date", RolloverDate)


def test_actual_current_capture_contains_complete_next_cohort(monkeypatch):
    """Future fees are genuinely published, not a synthetic price-year replacement."""
    from app.services.scraper.ulaw_qualifications import _verified_page
    freeze_rollover(monkeypatch)
    authority, proofs = _verified_page(live_capture(), URL)
    assert authority["fee_year"] == 2027
    assert {(o["study_variant"], o["campus"], o["amount"]) for o in authority["selected"]} == {
        ("PG Dip", "London", 13500), ("PG Dip", "Outside of London", 12500),
        ("PG Cert", "London", 6750), ("PG Cert", "Outside of London", 6300),
    }
    for proof in proofs.values():
        assert proof["fee_term"] == "Full Course"
        assert (proof["cohort_start"], proof["cohort_end"]) == ("2027-06-01", "2028-05-31")
        assert proof["locations"] == ["Bristol", "London Moorgate"]
    assert {e["intake"] for e in proofs["PG Cert"]["start_dates"]} == {"October 2027"}
    assert {e["intake"] for e in proofs["PG Dip"]["start_dates"]} == {"October 2027", "February 2028"}


@pytest.mark.asyncio
async def test_verified_live_cohort_rollover_keeps_award_and_existing_offering_ids(db, monkeypatch):
    freeze_cohort(monkeypatch)
    html = live_capture()
    mock_official_fetch(monkeypatch, html)
    uni, _, original_id = await seed(db)
    first = await route.approve_selected(route.ApproveSelectedBody(courseIds=[original_id]), db,
                                         {"email": "test-reviewer"})
    assert not first["failed"], first
    courses, offerings = await published(db, uni)
    course_ids = {c.name: c.id for c in courses}
    offering_ids = {(o.course_id, o.location): o.id for o in offerings}
    freeze_rollover(monkeypatch)
    values = captured_values()
    authority = ulaw_fees.parse_course_fees(html, URL)
    values["fee_year"] = 2027
    values["extraction_method"]["fee_variants"] = authority
    job_id = str(uuid4())
    db.add(ScrapeRuntimeJob(runtime_job_id=job_id, university_id=uni,
                           job_type="scrape", status="completed"))
    await db.flush()
    row = ScrapedCourse(university_id=uni, scrape_job_id=job_id, **values)
    db.add(row)
    await db.flush()
    next_result = await route.approve_selected(route.ApproveSelectedBody(courseIds=[row.id]), db,
                                               {"email": "test-reviewer"})
    assert not next_result["failed"], next_result
    courses, offerings = await published(db, uni)
    assert {c.name: c.id for c in courses} == course_ids
    assert len(offerings) == 4
    assert all(o.id == offering_ids[(o.course_id, o.location)]
               for o in offerings if (o.course_id, o.location) in offering_ids)
    assert {(c.name, o.location, o.fee_amount) for c in courses for o in offerings if o.course_id == c.id} == {
        ("PG Dip Legal Technology", "London Moorgate", 13500),
        ("PG Dip Legal Technology", "Bristol", 12500),
        ("PG Cert Legal Technology", "London Moorgate", 6750),
        ("PG Cert Legal Technology", "Bristol", 6300),
    }
    assert all(o.fee_year == 2027 for o in offerings)
    for staged_id in next_result["approvedIds"]:
        staged = await db.get(ScrapedCourse, staged_id)
        proof = staged.extraction_method[QUALIFICATION_SCOPE]["verified_source"]
        assert proof["response_sha256"] == sha256(html.encode()).hexdigest()
        assert len(proof["authority"]["selected"]) == 4


@pytest.mark.parametrize("damage", ["no_award_intakes", "online", "no_window", "term", "missing_price"])
def test_next_cohort_requires_joint_fee_and_award_owned_physical_contract(monkeypatch, damage):
    from bs4 import BeautifulSoup
    from app.services.scraper.ulaw_qualifications import _verified_page
    freeze_rollover(monkeypatch)
    soup = BeautifulSoup(live_capture(), "html.parser")
    if damage in {"no_award_intakes", "online"}:
        item = next(i for i in soup.select("#accordion-bcz .accordion__item")
                    if i.select_one("h4").get_text(strip=True) == "October 2027")
        if damage == "no_award_intakes":
            item.decompose()
        else:
            for li in item.select(".accordion__body li li"):
                li.string = "Part-time: Online"
    html = str(soup)
    if damage == "no_window":
        html = html.replace("1 June 2027 - 31 May 2028", "dates to be confirmed")
    elif damage == "term":
        html = html.replace("PG Cert Non-domestic students", "PG Cert Non-domestic students per year")
    elif damage == "missing_price":
        html = html.replace("£6,750", "TBC")
    assert _verified_page(html, URL) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["window", "year", "amount", "proof", "expired", "missing_capture"])
async def test_next_cohort_persisted_source_mismatch_is_rejected(db, monkeypatch, damage):
    from app.services.scraper.ulaw_qualifications import split_pending_qualifications, validate_qualification_scope
    freeze_rollover(monkeypatch)
    html = live_capture()
    mock_official_fetch(monkeypatch, html)
    values = captured_values()
    values["fee_year"] = 2027
    values["extraction_method"]["fee_variants"] = ulaw_fees.parse_course_fees(html, URL)
    _, _, row_id = await seed(db, values)
    row = await db.get(ScrapedCourse, row_id)
    result = await split_pending_qualifications(db, row)
    assert result["status"] == "split"
    assert validate_qualification_scope(row)
    metadata = deepcopy(row.extraction_method)
    if damage == "window":
        metadata["campus_authority"]["cohort_start"] = "2026-06-01"
    elif damage == "year":
        row.fee_year = 2026
    elif damage == "amount":
        metadata["fee_variants"]["selected"][0]["amount"] += 1
    elif damage == "proof":
        metadata["campus_authority"]["start_dates"][0]["locations"] = ["Online"]
    elif damage == "missing_capture":
        metadata[QUALIFICATION_SCOPE].pop("verified_source")
    else:
        class ExpiredDate(date):
            @classmethod
            def today(cls):
                return cls(2028, 6, 1)
        monkeypatch.setattr(ulaw_fees, "date", ExpiredDate)
    row.extraction_method = metadata
    assert not validate_qualification_scope(row)


@pytest.mark.asyncio
async def test_legacy_pinned_current_cohort_remains_valid_without_inventing_provenance(db, monkeypatch):
    from app.services.scraper.ulaw_qualifications import split_pending_qualifications, validate_qualification_scope
    freeze_cohort(monkeypatch)
    mock_official_fetch(monkeypatch)
    _, _, row_id = await seed(db)
    row = await db.get(ScrapedCourse, row_id)
    assert (await split_pending_qualifications(db, row))["status"] == "split"
    metadata = deepcopy(row.extraction_method)
    metadata[QUALIFICATION_SCOPE].pop("verified_source")
    row.extraction_method = metadata
    assert validate_qualification_scope(row)
    assert "verified_source" not in row.extraction_method[QUALIFICATION_SCOPE]


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["coordinated_fee", "coordinated_intake", "unavailable"])
async def test_pending_promotion_reverifies_source_not_mutable_capture_hashes(db, monkeypatch, damage):
    from app.services.scraper.ulaw_qualifications import (
        _contract_hash, split_pending_qualifications, validate_qualification_scope,
    )
    from app.services.scraper.campus_fee_split import split_pending_course
    from app.services.scraper.approve_course import ApprovalValidationError, approve_scraped_course

    real_client = httpx.AsyncClient
    freeze_rollover(monkeypatch)
    html = live_capture()
    mock_official_fetch(monkeypatch, html)
    values = captured_values()
    values["fee_year"] = 2027
    values["extraction_method"]["fee_variants"] = ulaw_fees.parse_course_fees(html, URL)
    uni, _, row_id = await seed(db, values)
    row = await db.get(ScrapedCourse, row_id)
    split = await split_pending_qualifications(db, row)
    cert = await db.get(ScrapedCourse, split["courseIds"][1])
    assert (await split_pending_course(db, cert))["status"] == "split"
    assert cert.course_location == "London Moorgate"
    metadata = deepcopy(cert.extraction_method)
    source = metadata[QUALIFICATION_SCOPE]["verified_source"]
    genuine_hash = source["response_sha256"]
    if damage == "coordinated_fee":
        for authority in (metadata["fee_variants"], source["authority"]):
            for option in authority["options"] + authority["selected"]:
                if option["study_variant"] == "PG Cert" and option["campus"] == "London" and option["year"] == 2027:
                    option["amount"] = 7777
                    option["snippet"] = option["snippet"].replace("6,750", "7,777")
        metadata["fee_variants"]["international_fee"] = cert.international_fee = 7777
    elif damage == "coordinated_intake":
        proof = deepcopy(metadata["campus_authority"])
        forged = deepcopy(proof["start_dates"][0])
        forged["intake"] = "February 2028"
        proof["start_dates"].append(forged)
        proof["snippet"] = " | ".join(e["snippet"] for e in proof["start_dates"])
        metadata["campus_authority"] = proof
        source["proofs"]["PG Cert"] = deepcopy(proof)
        cert.intake_months = ["October", "February"]
    else:
        monkeypatch.setattr(httpx, "AsyncClient", real_client)
        mock_official_fetch(monkeypatch, html, 503)
    source["contract_sha256"] = _contract_hash(source["authority"], source["proofs"])
    assert source["response_sha256"] == genuine_hash
    cert.extraction_method = metadata
    # Deliberately demonstrate that consistency + recomputed checksum is not
    # external authority. The shared async promotion boundary must stop it.
    assert validate_qualification_scope(cert)
    await db.flush()
    with pytest.raises(ApprovalValidationError) as rejected:
        await approve_scraped_course(db, cert, actor="test-reviewer", commit=False)
    assert rejected.value.reason_code == (
        "official_source_unavailable" if damage == "unavailable" else "changed_cohort"
    )
    assert cert.status == "pending" and cert.course_id is None
    assert not (await published(db, uni))[0]
    result = await route.approve_selected(
        route.ApproveSelectedBody(courseIds=[cert.id], force=True), db, {"email": "test-reviewer"},
    )
    assert result["approvedIds"] == [] and result["failed"]
    assert not (await published(db, uni))[0]


@pytest.mark.asyncio
async def test_approved_historical_retries_keep_ids_after_cohort_expiry_without_fetch(db, monkeypatch):
    from app.services.scraper.approve_course import approve_scraped_course
    from app.services.scraper.ulaw_qualifications import validate_qualification_scope

    freeze_cohort(monkeypatch)
    mock_official_fetch(monkeypatch)
    uni, _, row_id = await seed(db)
    result = await route.approve_selected(
        route.ApproveSelectedBody(courseIds=[row_id]), db, {"email": "test-reviewer"},
    )
    assert not result["failed"]
    courses, offerings = await published(db, uni)
    course_ids, offering_ids = {c.id for c in courses}, {o.id for o in offerings}
    freeze_rollover(monkeypatch)
    def no_fetch(**kwargs):
        pytest.fail("Historical approved retries must not fetch or re-promote")
    monkeypatch.setattr(httpx, "AsyncClient", no_fetch)
    for staged_id in result["approvedIds"]:
        staged = await db.get(ScrapedCourse, staged_id)
        assert not validate_qualification_scope(staged)  # expired for NEW promotion
        retry = await approve_scraped_course(db, staged, actor="test-reviewer")
        assert retry["course_id"] in course_ids and retry["reason"] == "Already approved"
    retried = await route.approve_selected(
        route.ApproveSelectedBody(courseIds=result["approvedIds"]), db, {"email": "test-reviewer"},
    )
    assert not retried["failed"] and set(retried["approvedIds"]) == set(result["approvedIds"])
    courses, offerings = await published(db, uni)
    assert {c.id for c in courses} == course_ids
    assert {o.id for o in offerings} == offering_ids
    assert all(o.fee_year == 2026 for o in offerings)


@pytest.mark.asyncio
async def test_single_approval_cannot_publish_unsplit_joint_award_after_fee_selection(db, monkeypatch):
    """The shared service must protect every endpoint, not just approve-selected."""
    from app.routers.scrape import _FeeSelectionBody, staged_approve, staged_fee_selection
    from app.services.scraper.approve_course import ApprovalValidationError, approve_scraped_course

    freeze_cohort(monkeypatch)
    uni, _, original_id = await seed(db)
    row = await db.get(ScrapedCourse, original_id)
    evidence = (await db.execute(select(ScrapedFieldEvidence).where(
        ScrapedFieldEvidence.scraped_course_id == original_id,
        ScrapedFieldEvidence.field_key == "international_fee",
    ))).scalar_one()
    evidence.raw_text = json.dumps(row.extraction_method["fee_variants"], ensure_ascii=False)
    await db.flush()
    state = fee_selection(row)
    option = next(o for o in state["options"] if o["studyVariant"] == "PG Cert"
                  and o["campus"] == "London" and o["year"] == 2026)
    choice = await staged_fee_selection(original_id, _FeeSelectionBody(
        snapshotToken=state["snapshotToken"], optionId=option["optionId"],
    ), db, {"email": "test-reviewer"})
    assert choice["success"]
    row = await db.get(ScrapedCourse, original_id)
    assert fee_selection(row)["selectedOptionId"] == option["optionId"]
    assert (row.international_fee, row.fee_year, row.currency, row.fee_term) == (
        6600, 2026, "GBP", "Full Course",
    )
    assert not (row.extraction_method or {}).get(QUALIFICATION_SCOPE)
    with pytest.raises(ApprovalValidationError):
        await approve_scraped_course(db, row, actor="test-reviewer")
    with pytest.raises(HTTPException) as exc:
        await staged_approve(original_id, db, {"email": "test-reviewer"}, {"force": True})
    assert exc.value.status_code == 422
    await db.refresh(row)
    assert row.status == "pending" and row.course_id is None
    assert not (await published(db, uni))[0]


@pytest.mark.asyncio
async def test_missing_diploma_february_is_rejected_before_joint_approval(db, monkeypatch):
    from app.services.scraper.ulaw_qualifications import split_pending_qualifications

    freeze_cohort(monkeypatch)
    mock_official_fetch(monkeypatch)
    uni, _, original_id = await seed(db)
    diploma = await db.get(ScrapedCourse, original_id)
    split = await split_pending_qualifications(db, diploma, actor="test-reviewer")
    assert split["status"] == "split"
    assert diploma.intake_months == ["October", "February"]
    diploma.intake_months = ["October"]
    await db.flush()

    result = await route.approve_selected(route.ApproveSelectedBody(courseIds=[original_id], force=True),
                                          db, {"email": "test-reviewer"})
    assert result["approvedIds"] == [] and result["failed"], result
    assert not (await published(db, uni))[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["award", "campus", "staged_intake", "proof_intake"])
async def test_tampered_award_scope_and_mixed_refresh_cannot_add_certificate_bristol(
    db, monkeypatch, tamper,
):
    from app.services.scraper.campus_fee_split import scope_refresh_payload
    from app.services.scraper.ulaw_qualifications import split_pending_qualifications

    freeze_cohort(monkeypatch)
    mock_official_fetch(monkeypatch)
    uni, _, original_id = await seed(db)
    original = await db.get(ScrapedCourse, original_id)
    split = await split_pending_qualifications(db, original, actor="test-reviewer")
    assert split["status"] == "split" and len(split["courseIds"]) == 2
    cert = await db.get(ScrapedCourse, split["courseIds"][1])
    assert cert.course_location == "London Moorgate"
    assert cert.intake_months == ["October"]
    assert cert.extraction_method["campus_authority"]["locations"] == ["London Moorgate"]
    incoming = captured_values()
    incoming["course_location"] = "Bristol, London Moorgate"
    incoming["extraction_method"]["campus_authority"] = {
        **cert.extraction_method["campus_authority"],
        "locations": ["Bristol", "London Moorgate"],
        "study_variant": "PG Dip",
    }
    refresh = scope_refresh_payload(cert, incoming)
    assert "course_location" not in refresh
    assert refresh["intake_months"] == ["October"]
    assert refresh["extraction_method"][QUALIFICATION_SCOPE] == cert.extraction_method[QUALIFICATION_SCOPE]
    assert refresh["extraction_method"]["campus_authority"]["locations"] == ["London Moorgate"]
    assert refresh["extraction_method"]["campus_authority"]["study_variant"] == "PG Cert"
    assert {o["study_variant"] for o in refresh["extraction_method"]["fee_variants"]["selected"]} == {"PG Cert"}

    # Forged persisted scope and campus authority must not turn the genuine
    # outside-London fee into a Bristol Certificate intake.
    corrupted = deepcopy(cert.extraction_method)
    if tamper == "award":
        corrupted[QUALIFICATION_SCOPE]["award"] = "PG Dip"
    elif tamper == "campus":
        corrupted["campus_authority"] = {
            **incoming["extraction_method"]["campus_authority"], "study_variant": "PG Cert",
        }
        cert.course_location = "Bristol, London Moorgate"
    elif tamper == "staged_intake":
        cert.intake_months = ["October", "February"]
    else:
        forged = deepcopy(corrupted["campus_authority"]["start_dates"][0])
        forged["intake"] = "February 2027"
        corrupted["campus_authority"]["start_dates"].append(forged)
        corrupted["campus_authority"]["snippet"] += " | " + forged["snippet"]
        cert.intake_months = ["October", "February"]
    cert.extraction_method = corrupted
    await db.flush()
    result = await route.approve_selected(route.ApproveSelectedBody(courseIds=[cert.id], force=True),
                                          db, {"email": "test-reviewer"})
    assert result["approvedIds"] == [] and result["failed"], result
    assert not (await published(db, uni))[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["unavailable", "wrong_route", "wrong_price", "missing_award",
                                    "wrong_name", "missing_campus"])
async def test_bad_live_evidence_cannot_publish_even_with_force(db, monkeypatch, damage):
    freeze_cohort(monkeypatch)
    values = captured_values()
    uni, job, original_id = await seed(db, values)
    html = {
        "unavailable": HTML,
        "wrong_route": HTML.replace(URL, URL + "other/"),
        "wrong_price": HTML.replace("13,150", "99,999"),
        "missing_award": HTML.replace("PG Cert Non-domestic students", "PG Cert Domestic students"),
        "wrong_name": HTML.replace("<h1 class=\"mb-3\">Legal Technology</h1>", "<h1>Access denied</h1>"),
        "missing_campus": HTML.replace('<a href="/locations/bristol/" >Bristol</a>, ', ""),
    }[damage]
    mock_official_fetch(monkeypatch, html, 503 if damage == "unavailable" else 200)
    before = deepcopy(values)
    result = await route.approve_selected(route.ApproveSelectedBody(courseIds=[original_id], force=True),
                                          db, {"email": "test-reviewer"})
    assert result["approvedIds"] == [] and len(result["failed"]) == 1, result
    assert result["failed"][0]["error"]
    row = await db.get(ScrapedCourse, original_id)
    await db.refresh(row)
    assert row.status == "pending" and row.course_id is None
    assert row.course_name == before["course_name"]
    assert row.extraction_method == before["extraction_method"]
    assert not (await published(db, uni))[0]
    assert not (await db.execute(select(ScrapedCourse).where(
        ScrapedCourse.university_id == uni, ScrapedCourse.scrape_job_id == job,
        ScrapedCourse.id != original_id
    ))).scalars().all()
    assert len((await db.execute(select(FieldConflict).where(
        FieldConflict.scraped_course_id == original_id
    ))).scalars().all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["cohort", "fee_url", "missing_selection", "wrong_award"])
async def test_stale_stored_authority_fails_closed_without_writes(db, monkeypatch, stale):
    freeze_cohort(monkeypatch)
    mock_official_fetch(monkeypatch)
    values = captured_values()
    authority = values["extraction_method"]["fee_variants"]
    if stale == "cohort":
        values["fee_year"] = authority["fee_year"] = 2027
        for option in authority["selected"]:
            option["year"] = 2027
    elif stale == "fee_url":
        authority["selected"][0]["source_url"] = URL + "different/"
    elif stale == "missing_selection":
        authority["selected"] = authority["selected"][:-1]
    else:
        authority["selected"][0]["study_variant"] = "MSc Legal Technology"
    uni, _, original_id = await seed(db, values)
    result = await route.approve_selected(route.ApproveSelectedBody(courseIds=[original_id], force=True),
                                          db, {"email": "test-reviewer"})
    assert not result["approvedIds"] and result["failed"], result
    original = await db.get(ScrapedCourse, original_id)
    await db.refresh(original)
    assert original.status == "pending" and original.course_id is None
    assert original.extraction_method == values["extraction_method"]
    assert not (await published(db, uni))[0]


@pytest.mark.asyncio
async def test_mixed_unrelated_fee_variant_and_reextraction_keep_review_boundary(db, monkeypatch):
    freeze_cohort(monkeypatch)
    mock_official_fetch(monkeypatch)
    values = captured_values()
    unrelated = deepcopy(values["extraction_method"]["fee_variants"]["selected"][0])
    unrelated["study_variant"] = "MSc Legal Technology"
    values["extraction_method"]["fee_variants"]["selected"].append(unrelated)
    values["extraction_method"]["fee_variants"]["options"].append(deepcopy(unrelated))
    uni, _, original_id = await seed(db, values)
    failed = await route.approve_selected(route.ApproveSelectedBody(courseIds=[original_id], force=True),
                                          db, {"email": "test-reviewer"})
    assert failed["approvedIds"] == [] and failed["failed"], failed
    assert (await db.get(ScrapedCourse, original_id)).status == "pending"
    assert not (await published(db, uni))[0]
