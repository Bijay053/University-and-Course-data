from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.extractors.fee import extract


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