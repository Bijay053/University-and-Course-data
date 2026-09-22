from pathlib import Path

from app.services.scraper.extractors.londonmet_academic import (
    apply_fill_only,
    extract_fields,
)

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://www.londonmet.ac.uk/courses"


def test_uk_lower_second_degree_is_qualification_not_numeric_score():
    html = '<section id="entry-requirements"><ul><li>a lower second class (2:2) UK degree (or equivalent) in Computing</li></ul></section>'
    result = extract_fields(html, BASE + "/postgraduate/computer-networking/")
    assert result["academic_level"] == "Bachelor's degree"
    assert "academic_score" not in result


def test_academics_run_with_deterministic_extractors_before_ai():
    from app.services.scraper.pipelines.single_course import _EXTRACTORS
    assert any(module.__name__.endswith(".londonmet_academic") for module, _ in _EXTRACTORS)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_standard_undergraduate_extracts_typed_ucas_and_prose_requirements():
    result = extract_fields(
        _fixture("londonmet_accounting_entry.html"),
        f"{BASE}/undergraduate/accounting-and-finance---ba-hons/",
    )

    assert result["academic_level"] == "Year 12"
    assert result["academic_score"] == 96.0
    assert result["score_type"] == "UCAS Points"
    assert "grades CCC in three A levels" in result["other_requirement"]
    assert "English Language and Mathematics GCSEs" in result["other_requirement"]
    assert "96 UCAS" not in result["other_requirement"]
    assert "September start" not in result["other_requirement"]


def test_foundation_and_portfolio_variants_use_course_owned_requirements():
    foundation = extract_fields(
        _fixture("londonmet_foundation_entry.html"),
        f"{BASE}/undergraduate/accounting-and-finance-including-foundation-year---ba-hons/",
    )
    architecture = extract_fields(
        _fixture("londonmet_architecture_entry.html"),
        f"{BASE}/undergraduate/architecture---ba-hons/",
    )

    assert foundation["academic_score"] == 32.0
    assert "at least one A level" in foundation["other_requirement"]
    assert architecture["academic_score"] == 120.0
    assert "portfolio of work" in architecture["other_requirement"]
    assert "attend an interview" in architecture["other_requirement"]


def test_postgraduate_classification_stays_prose_not_numeric_score():
    result = extract_fields(
        _fixture("londonmet_mba_entry.html"),
        f"{BASE}/postgraduate/master-of-business-administration---mba/",
    )

    assert result["academic_level"] == "Bachelor's degree"
    assert "academic_score" not in result
    assert "score_type" not in result
    assert "minimum of a 2.2 for an honours degree" in result["other_requirement"]
    assert "two years' work experience" in result["other_requirement"]
    assert "detailed personal statement" in result["other_requirement"]


def test_numeric_ucas_range_uses_lower_end_but_untyped_numbers_are_ignored():
    html = """
    <section id="entry-requirements">
      <ul>
        <li>Applicants normally require 112–120 UCAS tariff points.</li>
        <li>A portfolio of 20 pieces and an interview are required.</li>
      </ul>
    </section>
    """
    result = extract_fields(html, f"{BASE}/undergraduate/example/")
    assert result["academic_score"] == 112.0
    assert result["score_type"] == "UCAS Points"

    untyped = html.replace("112–120 UCAS tariff points", "a 2.1 honours degree")
    result = extract_fields(untyped, f"{BASE}/postgraduate/example/")
    assert "academic_score" not in result
    assert "score_type" not in result


def test_extraction_requires_bounded_panel_and_londonmet_host():
    html = """
    <nav>48 UCAS points</nav>
    <main><h2>Entry requirements</h2><p>96 UCAS points</p></main>
    """
    assert extract_fields(html, f"{BASE}/undergraduate/example/") == {}

    panel = _fixture("londonmet_accounting_entry.html")
    assert extract_fields(panel, "https://example.edu/courses/example/") == {}


def test_post_ai_application_is_fill_only_and_pairs_matching_score_type():
    html = _fixture("londonmet_accounting_entry.html")
    url = f"{BASE}/undergraduate/accounting-and-finance---ba-hons/"
    payload = {"academic_level": "Other", "academic_score": 96.0}
    evidence: list[dict] = []

    applied = apply_fill_only(payload, html, url=url, evidence=evidence)

    assert payload["academic_level"] == "Other"
    assert payload["academic_score"] == 96.0
    assert payload["score_type"] == "UCAS Points"
    assert "academic_level" not in applied
    assert applied["score_type"]["new"] == "UCAS Points"
    assert evidence[0]["source_url"] == url
    assert evidence[0]["snippet"]

    conflicting = {"academic_score": 48.0}
    apply_fill_only(conflicting, html, url=url)
    assert conflicting == {
        "academic_score": 48.0,
        "academic_level": "Year 12",
        "other_requirement": (
            "a minimum of grades CCC in three A levels; "
            "English Language and Mathematics GCSEs at grade C/grade 4 "
            "or above (or equivalent)"
        ),
    }