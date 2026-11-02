from app.services.scraper.extractors.hsu_facts import apply_course_authority
import pytest
import pytest_asyncio

URL = "https://www.hsu.ac.uk/course/msc-chiropractic-pre-registration/"
LOCATION = """<nav>Bournemouth Campus London Campus Reading Campus</nav>
<article><span class="label">Location</span><span class="value">Bournemouth</span></article>"""
FEES = """<h3>Tuition Fees 2025/26</h3><h4>International</h4>
<p>£19,755 per annum (Full time)</p>
<h3>Tuition Fees 2026/27</h3><h4>UK/ROI/Channel Islands</h4>
<p>£9,790 (Full time)</p><h4>International</h4>
<p>£17, 684 (Full time)</p><p>£1956 per 10 credits (Part-time)</p>"""


@pytest.fixture(autouse=True)
def no_live_gemini(monkeypatch):
    # Optional enrichment can run even with use_ai_fallback=False.
    # Deterministic authority/persistence tests must not depend on live AI.
    monkeypatch.setattr("app.services.ai.gemini_client._client", lambda: None)


def apply(html, url=URL):
    payload = {"course_location": "Bournemouth Campus London Campus Reading Campus",
               "international_fee": 9790, "currency": "AUD", "fee_term": "Annual"}
    evidence = [{"field_key": "international_fee", "value": 9790}]
    apply_course_authority(html, url, payload, evidence)
    return payload, evidence


def test_current_international_fee_and_course_location_replace_contamination():
    payload, evidence = apply(LOCATION + FEES)
    assert payload["course_location"] == "Bournemouth"
    assert payload["international_fee"] == 17684
    assert payload["currency"] == "GBP"
    assert payload["fee_year"] == 2026
    assert payload["fee_term"] == "Annual"
    assert evidence[0]["decision_status"] == "needs_review"


def test_unpublished_overseas_price_is_not_domestic_price():
    payload, _ = apply(LOCATION + """<h3>Tuition Fees 2027/28</h3>
    <h4>UK/ROI/Channel Islands</h4>
    <p>£10,050 Fees for overseas students are still to be confirmed.</p>""")
    assert payload["international_fee"] is None
    assert payload["fee_term"] is None


def test_latest_unpublished_section_does_not_reuse_older_fee():
    payload, _ = apply(FEES + "<h3>Tuition Fees 2027/28</h3><h4>International</h4><p>To be confirmed</p>")
    assert payload["international_fee"] is None


def test_navigation_and_absent_international_evidence_fail_closed():
    payload, _ = apply("<nav>London Campus</nav>")
    assert payload["course_location"] is None
    assert payload["international_fee"] is None


def test_other_hosts_and_noncourse_pages_untouched():
    for url in ("https://other.ac.uk/course/test/", "https://www.hsu.ac.uk/study/"):
        payload, _ = apply(LOCATION + FEES, url)
        assert payload["international_fee"] == 9790
        assert payload["currency"] == "AUD"


def test_explicit_total_is_not_annualised():
    payload, _ = apply(LOCATION + "<h3>Tuition Fees 2026/27</h3><h4>International</h4><p>£24,000 total course fee</p>")
    assert payload["fee_term"] == "Full Course"


def test_conflicting_current_prices_are_not_guessed():
    payload, _ = apply(FEES + "<p>£20,000 (Full time)</p>")
    assert payload["international_fee"] is None


@pytest.mark.asyncio
async def test_full_pipeline_preserves_hsu_authority_and_selected_evidence(monkeypatch):
    from app.services.scraper.config.context import current_uni_config
    from app.services.scraper.config.loader import load_uni_config
    from app.services.scraper.pipelines.single_course import extract_course

    async def no_ai(*args, **kwargs):
        return {}, 0.0, 0, 0, {"skipped": True}

    monkeypatch.setattr("app.services.scraper.extractors.gemini_primary.extract_primary", no_ai)
    config = load_uni_config(
        slug="hsu", name="Health Sciences University", university_id=101,
        scrape_url="https://www.hsu.ac.uk", create_missing_stub=False,
    )
    config.extraction.skip_per_course_browser = True
    token = current_uni_config.set(config)
    try:
        html = """<main><h1>MSc Chiropractic (Pre-registration)</h1>
        <p>Full-time on campus. Duration: 2 years. Start date: September.
        IELTS 7.0 with no band below 6.5.</p>""" + LOCATION + FEES + "</main>"
        result = await extract_course(
            URL, html=html, country="United Kingdom", use_ai_fallback=False,
        )
    finally:
        current_uni_config.reset(token)
    assert result["payload"]["course_location"] == "Bournemouth"
    assert result["payload"]["international_fee"] == 17684
    assert result["payload"]["currency"] == "GBP"
    assert result["payload"]["fee_year"] == 2026
    assert result["payload"]["fee_term"] == "Annual"
    for field, method in (
        ("international_fee", "fee.hsu_international_tuition"),
        ("course_location", "location.hsu_course_fact"),
    ):
        assert any(
            row["field_key"] == field and row.get("method") == method
            and row.get("decision_status") == "selected"
            for row in result["evidence"]
        )


@pytest.fixture(scope="module")
def hsu_postgres():
    """A private socket-only cluster: never use development/production credentials."""
    from pathlib import Path
    import shutil
    import subprocess
    import tempfile

    for binary in ("initdb", "pg_ctl"):
        assert shutil.which(binary), f"{binary} required for HSU persistence tests"
    with tempfile.TemporaryDirectory(prefix="hsu-pg-") as directory:
        root = Path(directory)
        data = root / "data"
        subprocess.run(
            ["initdb", "-D", str(data), "-A", "trust", "-U", "hsu_test"],
            check=True, capture_output=True, timeout=60,
        )
        try:
            subprocess.run(
                ["pg_ctl", "-D", str(data), "-l", str(root / "postgres.log"),
                 "-o", f"-h '' -k {root}", "-w", "start"],
                check=True, capture_output=True, timeout=60,
            )
            yield {"host": str(root), "user": "hsu_test", "database": "postgres"}
        finally:
            if (data / "postmaster.pid").exists():
                subprocess.run(
                    ["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                    check=True, capture_output=True, timeout=60,
                )
            assert not (data / "postmaster.pid").exists()


@pytest_asyncio.fixture
async def hsu_database(hsu_postgres):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from app.database import Base

    engine = create_async_engine(
        "postgresql+asyncpg://", connect_args=hsu_postgres, poolclass=NullPool,
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                yield async_sessionmaker(
                    connection, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fees,amount,term,year", [
    (FEES, 17684, "Annual", 2026),
    ("<h3>Tuition Fees 2027/28</h3><h4>International</h4>"
     "<p>£24,000 total course fee</p>", 24000, "Full Course", 2027),
    ("<h3>Tuition Fees 2027/28</h3><h4>UK/ROI/Channel Islands</h4>"
     "<p>£10,050 Fees for overseas students are still to be confirmed.</p>",
     None, None, None),
], ids=["annual", "full-course", "unpublished-replaces-domestic"])
async def test_hsu_stage_and_review_database_roundtrip(
    hsu_database, monkeypatch, fees, amount, term, year,
):
    from uuid import uuid4
    from sqlalchemy import select
    from app.models import University, ScrapedCourse, ScrapedFieldEvidence
    from app.models.scrape_runtime import ScrapeRuntimeJob
    from app.routers.scrape import staged_review
    from app.services.scraper.stage_course import stage_course
    from app.services.scraper.config.context import current_uni_config
    from app.services.scraper.config.loader import load_uni_config
    from app.services.scraper.pipelines.single_course import extract_course

    async def no_ai(*args, **kwargs):
        return {}, 0.0, 0, 0, {"skipped": True}

    monkeypatch.setattr("app.services.scraper.extractors.gemini_primary.extract_primary", no_ai)
    config = load_uni_config(
        slug="hsu", name="Health Sciences University", university_id=101,
        scrape_url="https://www.hsu.ac.uk", create_missing_stub=False,
    )
    config.extraction.skip_per_course_browser = True
    token = current_uni_config.set(config)
    try:
        fresh = await extract_course(
            URL, html="<main><h1>MSc Chiropractic (Pre-registration)</h1>"
            "<p>Full-time on campus. Duration: 2 years. Start date: September. "
            "IELTS 7.0 with no band below 6.5.</p>" + LOCATION + fees + "</main>",
            country="United Kingdom", use_ai_fallback=False,
        )
    finally:
        current_uni_config.reset(token)

    async def stage_with_config(db, **kwargs):
        token = current_uni_config.set(config)
        try:
            return await stage_course(db, **kwargs)
        finally:
            current_uni_config.reset(token)

    job_id = f"hsu-roundtrip-{uuid4()}"
    old_job_id = f"hsu-legacy-{uuid4()}"
    async with hsu_database() as db:
        uni = University(name="HSU isolated roundtrip", country="United Kingdom", city="Bournemouth")
        db.add(uni)
        await db.flush()
        uni_id = uni.id
        db.add(ScrapeRuntimeJob(
            runtime_job_id=job_id, university_id=uni_id,
            job_type="scrape", status="completed",
        ))
        await db.flush()
        if amount is None:
            db.add(ScrapeRuntimeJob(
                runtime_job_id=old_job_id, university_id=uni_id,
                job_type="scrape", status="completed",
            ))
            await db.flush()
            old = await stage_with_config(
                db, scrape_job_id=old_job_id, university_id=uni_id,
                course_name="MSc Chiropractic (Pre-registration)", source_url=URL,
                payload={**fresh["payload"], "international_fee": 9790,
                         "currency": "GBP", "fee_year": 2026, "fee_term": "Annual"},
                evidence=[{
                    "field_key": "international_fee", "value": 9790,
                    "method": "legacy.domestic_price", "source_url": URL,
                    "snippet": "UK/ROI/Channel Islands £9,790",
                    "decision_status": "selected",
                }],
            )
            assert old.saved, old.reason
            old_id = old.scraped_course_id
        await db.commit()

    # New sessions force reads from PostgreSQL, not an ORM identity-map cache.
    async with hsu_database() as db:
        if amount is None:
            old_review = await staged_review(old_id, db)
            assert old_review["international_fee"] == 9790
            assert any(ev["selected"] for ev in old_review["evidence"])
        staged = await stage_with_config(
            db, scrape_job_id=job_id, university_id=uni_id,
            course_name="MSc Chiropractic (Pre-registration)", source_url=URL,
            payload=fresh["payload"], evidence=fresh["evidence"],
        )
        assert staged.saved, staged.reason
        row_id = staged.scraped_course_id
        await db.commit()

    async with hsu_database() as db:
        row = await db.get(ScrapedCourse, row_id)
        assert (row.international_fee, row.currency, row.fee_term, row.fee_year) == (
            amount, "GBP", term, year,
        )
        assert row.course_location == "Bournemouth"
        review = await staged_review(row_id, db)
        assert review["course"]["internationalFee"] == amount
        assert review["course"]["currency"] == "GBP"
        assert review["course"]["feeTerm"] == term
        assert review["course"]["feeYear"] == year
        assert review["course"]["courseLocation"] == "Bournemouth"
        selected = [ev for ev in review["evidence"] if ev["selected"]]
        location = [ev for ev in selected if ev["field_key"] == "course_location"]
        assert len(location) == 1
        assert location[0]["extraction_method"] == "location.hsu_course_fact"
        assert location[0]["source_url"] == URL
        assert location[0]["decision_status"] == "selected"
        fee_evidence = [ev for ev in selected if ev["field_key"] == "international_fee"]
        if amount is None:
            assert fee_evidence == []
            assert not any(ev["extraction_method"] == "legacy.domestic_price"
                           and ev["selected"] for ev in review["evidence"])
            rows = (await db.scalars(select(ScrapedCourse).where(
                ScrapedCourse.university_id == uni_id,
            ))).all()
            assert [item.id for item in rows] == [row_id]
            stale_selected = (await db.scalars(select(ScrapedFieldEvidence).where(
                ScrapedFieldEvidence.scraped_course_id == old_id,
                ScrapedFieldEvidence.selected.is_(True),
            ))).all()
            assert stale_selected == []
        else:
            assert len(fee_evidence) == 1
            # Equal-price candidates can select the generic international
            # excerpt. Check the actual extraction winner survives unchanged,
            # rather than imposing a different winner during persistence.
            winner = next(ev for ev in fresh["evidence"]
                          if ev["field_key"] == "international_fee"
                          and ev.get("decision_status") == "selected")
            assert fee_evidence[0]["extraction_method"] == winner["method"]
            assert float(fee_evidence[0]["candidate_value"]) == amount
            assert fee_evidence[0]["source_url"] == URL
            assert fee_evidence[0]["decision_status"] == "selected"
            assert str(year) in fee_evidence[0]["snippet"]
            assert any(ev["extraction_method"] == "fee.hsu_international_tuition"
                       for ev in review["evidenceByField"]["international_fee"])
