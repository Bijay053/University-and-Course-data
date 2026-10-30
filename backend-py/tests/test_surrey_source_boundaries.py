"""Surrey catalogue URLs and English conversion reference regressions."""
import asyncio
import re

from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.extractors import english_test
from app.services.scraper.central_pages import _parse_english_page_html_async


def config():
    return load_uni_config(
        slug="surrey", name="University of Surrey",
        scrape_url="https://www.surrey.ac.uk", university_id=102,
        create_missing_stub=False,
        db_scrape_config={"admin_config": {
            "discovery": {"allow_url_patterns": ["/postgraduate/.*phd$"],
                          "seed_urls": ["https://www.surrey.ac.uk/postgraduate/research"],
                          "expected_min_courses": 400},
            "extraction": {"english": {"default_pte": 60, "default_toefl": 80,
                                      "default_ielts": 6.0}},
        }},
    )


def test_catalogue_covers_all_levels_despite_stale_admin_config():
    cfg = config()
    for path in ("/undergraduate/psychology", "/undergraduate/psychology/2027",
                 "/postgraduate/accounting-and-finance-msc",
                 "/postgraduate/chemistry-phd"):
        assert any(re.search(p, path) for p in cfg.discovery.allow_url_patterns)
    assert len(cfg.discovery.seed_urls) == 3
    assert cfg.discovery.expected_min_courses == 200
    assert cfg.extraction.english.default_ielts is None
    assert cfg.extraction.english.default_pte is None
    assert cfg.extraction.english.default_toefl is None


def test_conversion_reference_cannot_supply_universal_requirements():
    html = """<h1>English language requirements</h1><table>
    <tr><th>IELTS Academic/IELTS Online</th><th>Equivalent scores</th></tr>
    <tr><td>7.0 overall with 6.5 in each band</td><td>185 overall
    Reading 162 Listening 162 Writing 169 Speaking 162</td></tr></table>"""
    url = "https://www.surrey.ac.uk/apply/international/english-language-requirements"
    assert asyncio.run(english_test.extract(html, url)) == []
    assert asyncio.run(_parse_english_page_html_async(html, url)) == {}


def test_course_owned_ielts_is_retained():
    results = asyncio.run(english_test.extract(
        "<main><h1>Chemistry PhD</h1><p>IELTS 6.5 with no band below 6.0</p></main>",
        "https://www.surrey.ac.uk/postgraduate/chemistry-phd",
    ))
    score = next(r.normalized for r in results if r.field_key == "ielts_overall")
    assert score["ielts_overall"] == 6.5
    assert score["ielts_reading"] == 6.0


def test_other_test_scores_cannot_be_emitted_as_ielts():
    assert english_test._emit("ielts", {"overall": 185, "reading": 162}, "") == []
    results = english_test._emit("ielts", {"overall": 6.5, "reading": 100, "writing": 6}, "")
    assert results[0].normalized == {"ielts_overall": 6.5, "ielts_writing": 6}
    assert english_test._emit("ielts", {"overall": 6.25}, "") == []
