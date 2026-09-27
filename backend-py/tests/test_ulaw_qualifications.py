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
