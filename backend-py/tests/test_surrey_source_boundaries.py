"""Surrey catalogue URLs and English conversion reference regressions."""
import asyncio
import ast
from pathlib import Path
import re
from types import SimpleNamespace

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
                          "skip_auto_api_discovery": False,
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
    assert cfg.discovery.skip_auto_api_discovery is True
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


def test_verified_catalogue_can_disable_only_the_auto_detected_api_route():
    from app.services.scraper.config.schema import DiscoveryConfig
    path = Path(__file__).resolve().parents[1] / "app/services/scraper/orchestrator.py"
    branches = [
        node for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.If) and
        "skip_auto_api_discovery" in ast.unparse(node.test)
    ]
    assert len(branches) == 1
    condition = compile(ast.Expression(branches[0].test), "<auto-api-policy>", "eval")
    context = {
        "links": [], "_archive_only": False, "_is_mq_host": False,
        "_uni_cfg": SimpleNamespace(discovery=config().discovery),
        "uni_scrape_config": {"auto_config": {"_api_provider": "rest_json"}},
    }
    assert not eval(condition, {}, context)
    context["_uni_cfg"] = SimpleNamespace(discovery=DiscoveryConfig())
    assert eval(condition, {}, context)  # Other universities retain their route.
