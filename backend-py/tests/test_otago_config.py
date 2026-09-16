from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.config.context import current_uni_config
from app.services.scraper.extractors.fee import extract
from app.services.scraper.extractors import intake, location
from app.services.scraper.guards import should_stage_course
from app.services.scraper.pipelines.single_course import extract_course


def _config():
    return load_uni_config(
        slug="otago",
        university_id=2189,
        scrape_url="https://www.otago.ac.nz/study/qualifications",
        name="University of Otago",
    )


def test_otago_explicit_international_label_overrides_domestic_reject_marker():
    config = _config()
    fees = config.extraction.fees
    assert config.extraction.scrape_do_render is True
    assert config.extraction.force_browser is True
    assert "Domestic" in fees.reject_keywords
    assert "International fee" in fees.international_fee_keywords


async def test_otago_visible_fee_block_extracts_international_amount():
    html = """
      <section aria-label="Estimated fees">
        <h2>Estimated fees</h2>
        <p>Domestic fee 2026: NZ $13,000 – NZ $15,500</p>
        <p>International fee 2027: NZ $66,255</p>
      </section>
    """
    results = await extract(
        html,
        "https://www.otago.ac.nz/study/qualifications/master-of-music-coursework",
    )
    assert results
    best = max(results, key=lambda result: result.confidence)
    assert best.value == 66255
    assert best.normalized["currency"] == "NZD"
    assert "International fee 2027" in best.snippet


async def test_otago_structured_fee_metadata_extracts_international_amount():
    html = """
      <meta name="internationalFeesMin" content="66255">
      <meta name="domesticFeesMin" content="13000">
      <meta name="domesticFeesMax" content="15500">
      <meta name="internationalFeesYear" content="2027">
      <meta name="internationalFeesMax" content="">
    """
    results = await extract(
        html,
        "https://www.otago.ac.nz/study/qualifications/master-of-music-coursework",
    )
    assert len(results) == 1
    result = results[0]
    assert result.value == 66255
    assert result.normalized["currency"] == "NZD"
    assert result.normalized["fee_year"] == 2027
    assert result.method == "fee.explicit_international_meta"


async def test_otago_rejects_implausible_bare_metadata_fee():
    results = await extract(
        '<meta name="internationalFeesMin" content="1">',
        "https://www.otago.ac.nz/study/qualifications/example",
    )
    assert results == []


async def test_otago_metadata_fee_survives_pipeline_and_staging_gate():
    config = _config()
    token = current_uni_config.set(config)
    url = "https://www.otago.ac.nz/study/qualifications/master-of-music-coursework"
    html = """
      <html><head>
        <meta name="internationalFeesMin" content="66255">
        <meta name="internationalFeesYear" content="2027">
      </head><body><main>
        <h1>Master of Music (Coursework) (MMus(Coursework))</h1>
        <p>Domestic fee 2026: NZ $13,000 – NZ $15,500</p>
        <p>Duration: 1 year full-time.</p>
        <p>Intake: February.</p>
        <p>Location: Dunedin.</p>
        <p>Study mode: On Campus.</p>
        <p>IELTS overall score of 6.5 with no band below 6.0.</p>
      </main></body></html>
    """
    try:
        result = await extract_course(
            url,
            country="New Zealand",
            html=html,
            use_ai_fallback=False,
        )
        payload = result["payload"]
        assert payload["international_fee"] == 66255
        assert payload["currency"] == "NZD"
        assert payload["fee_year"] == 2027
        fee_evidence = [
            row for row in result["evidence"]
            if row.get("field_key") == "international_fee"
        ]
        assert any(
            row.get("method") == "fee.explicit_international_meta"
            and row.get("value") == 66255
            for row in fee_evidence
        )
        assert should_stage_course(payload["course_name"], payload, url) == (
            True,
            "accepted",
        )
    finally:
        current_uni_config.reset(token)


async def test_otago_course_metadata_is_authoritative_and_keeps_provenance():
    url = "https://www.otago.ac.nz/study/qualifications/bachelor-of-sustainability"
    html = """
      <html><head>
        <meta name="startDates"
          content="Semester 1 2027: 1&nbsp;March; Semester 2 2027: 12&nbsp;July">
        <meta name="location" content="Dunedin">
      </head><body>
        <nav>Our campuses: Auckland, Wellington, Christchurch, Dunedin</nav>
        <main><h1>Bachelor of Sustainability</h1>
          <p>Applications close 15 November. Start dates are shown below.</p>
        </main>
      </body></html>
    """
    location_results = await location.extract(html, url)
    intake_results = await intake.extract(html, url)
    assert location_results[0].value == "Dunedin"
    assert location_results[0].method == "location.otago_course_meta"
    assert intake_results[0].value == ["March", "July"]
    assert intake_results[0].method == "intake.otago_course_meta"

    config = _config()
    token = current_uni_config.set(config)
    try:
        result = await extract_course(url, country="New Zealand", html=html, use_ai_fallback=False)
        assert result["payload"]["course_location"] == "Dunedin"
        assert result["payload"]["intake_months"] == ["March", "July"]
        evidence = result["evidence"]
        assert any(
            row["method"] == "location.otago_course_meta"
            and "meta[name=\"location\"]" in row.get("snippet", "")
            for row in evidence
        )
        # Unrelated extractors must not gain an empty extras object.
        assert all("extras" not in row or row["extras"] for row in evidence)
    finally:
        current_uni_config.reset(token)


async def test_otago_empty_course_metadata_stays_empty():
    url = "https://www.otago.ac.nz/study/qualifications/example"
    html = """
      <meta name="startDates" content="">
      <meta name="location" content="">
      <nav>Campuses Auckland Wellington Christchurch Dunedin</nav>
      <p>Next intake March. Location: Auckland.</p>
    """
    config = _config()
    token = current_uni_config.set(config)
    try:
        result = await extract_course(url, country="New Zealand", html=html, use_ai_fallback=False)
        assert not result["payload"].get("course_location")
        assert not result["payload"].get("intake_months")
    finally:
        current_uni_config.reset(token)


async def test_otago_empty_metadata_clears_stage0_values_and_locks_fallbacks():
    url = "https://www.otago.ac.nz/study/qualifications/example"
    html = """
      <meta name="startDates" content="">
      <meta name="location" content="">
      <div class="guess">Auckland, Wellington, Christchurch, Dunedin</div>
      <p>Next intake March.</p>
    """
    config = _config()
    token = current_uni_config.set(config)
    try:
        result = await extract_course(
            url,
            country="New Zealand",
            html=html,
            use_ai_fallback=False,
            extraction_rules={
                "course_location": {"css": ".guess"},
                "intake_months": {"css": ".guess"},
            },
        )
        assert not result["payload"].get("course_location")
        assert not result["payload"].get("intake_months")
        assert result["payload"]["extraction_method"]["course_location"] == (
            "location.otago_course_meta:null"
        )
        assert result["payload"]["extraction_method"]["intake_months"] == (
            "intake.otago_course_meta:null"
        )
    finally:
        current_uni_config.reset(token)


async def test_otago_empty_metadata_blocks_mocked_ai_fallback(monkeypatch):
    from app.services.scraper.extractors import ai_fallback
    called = False

    async def _hallucinate(*args, **kwargs):
        nonlocal called
        called = True
        return {
            "course_location": "Auckland, Wellington, Christchurch, Dunedin",
            "intake_months": ["March", "July"],
        }

    monkeypatch.setattr(ai_fallback, "fill_missing", _hallucinate)
    url = "https://www.otago.ac.nz/study/qualifications/example"
    html = (
        '<meta name="startDates" content=""><meta name="location" content="">'
        '<p>' + ('Course information and admissions details. ' * 8) + '</p>'
    )
    config = _config()
    token = current_uni_config.set(config)
    try:
        result = await extract_course(url, country="New Zealand", html=html, use_ai_fallback=True)
        assert called
        assert not result["payload"].get("course_location")
        assert not result["payload"].get("intake_months")
    finally:
        current_uni_config.reset(token)


async def test_otago_empty_metadata_blocks_mocked_gemini_primary_aliases(monkeypatch):
    from app.services.scraper.extractors import gemini_primary
    called = False

    async def _primary(*args, **kwargs):
        nonlocal called
        called = True
        return (
            {
                "location_text": "Auckland, Wellington, Christchurch, Dunedin",
                "intake_text": "March, July",
            },
            0.1,
            1,
            1,
            {},
        )

    monkeypatch.setattr(gemini_primary, "extract_primary", _primary)
    url = "https://www.otago.ac.nz/study/qualifications/example"
    html = (
        '<meta name="startDates" content=""><meta name="location" content="">'
        '<p>' + ('Course information and admissions details. ' * 8) + '</p>'
    )
    config = _config()
    token = current_uni_config.set(config)
    try:
        result = await extract_course(url, country="New Zealand", html=html, use_ai_fallback=True)
        assert called
        payload = result["payload"]
        assert not payload.get("course_location")
        assert not payload.get("location_text")
        assert not payload.get("intake_months")
        assert not payload.get("intake_text")
    finally:
        current_uni_config.reset(token)


async def test_otago_metadata_absent_falls_through_and_scope_is_strict():
    html = '<p>Location: Auckland. Intake: March.</p>'
    assert await location.extract(
        html, "https://www.otago.ac.nz/study/qualifications/example"
    )
    assert await intake.extract(
        html, "https://www.otago.ac.nz/study/qualifications/example"
    )
    assert await location.extract(
        '<meta name="location" content="Dunedin">',
        "https://example.com/study/qualifications/example",
    ) == []
    assert await intake.extract(
        '<meta name="startDates" content="March">',
        "https://www.otago.ac.nz/study/subjects/example",
    ) == []


async def test_otago_empty_metadata_blocks_mocked_browser_merge(monkeypatch):
    import app.services.scraper.per_course_browser as browser

    called = False

    async def _browser_values(*args, **kwargs):
        nonlocal called
        called = True
        return (
            {
                "course_location": "Auckland, Wellington, Christchurch, Dunedin",
                "location_text": "Auckland",
                "intake_months": ["March", "July"],
            },
            [],
            None,
            True,
        )

    monkeypatch.setattr(browser, "maybe_browser_refetch", _browser_values)
    url = "https://www.otago.ac.nz/study/qualifications/example"
    html = '<meta name="startDates" content=""><meta name="location" content="">'
    config = _config()
    token = current_uni_config.set(config)
    try:
        result = await extract_course(url, country="New Zealand", html=html, use_ai_fallback=False)
        assert called
        assert not result["payload"].get("course_location")
        assert not result["payload"].get("location_text")
        assert not result["payload"].get("intake_months")
    finally:
        current_uni_config.reset(token)
async def test_otago_positive_metadata_owns_final_extraction_method():
    url = "https://www.otago.ac.nz/study/qualifications/example"
    html = """
      <meta name="location" content="Dunedin">
      <meta name="startDates" content="Semester 1 2027: 1 March">
      <div class="guess">Auckland, Wellington, Christchurch, Dunedin</div>
    """
    config = _config()
    token = current_uni_config.set(config)
    try:
        result = await extract_course(
            url, country="New Zealand", html=html, use_ai_fallback=False,
            extraction_rules={"course_location": {"css": ".guess"}},
        )
        methods = result["payload"]["extraction_method"]
        assert methods["course_location"] == "location.otago_course_meta"
        assert methods["intake_months"] == "intake.otago_course_meta"
    finally:
        current_uni_config.reset(token)