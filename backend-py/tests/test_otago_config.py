from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.config.context import current_uni_config
from app.services.scraper.extractors.fee import extract
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