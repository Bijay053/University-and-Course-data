"""Bug C: degree_level extractor.

Without this extractor the Review table's Level column showed "--" for every
staged course and ``auto_publish_status`` was permanently stuck on
``pending_review`` (degree_level is a hard precondition for auto-publish).
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.scraper.extractors import degree_level


def _classify(name: str, html: str = "") -> str | None:
    out, _, _ = degree_level.classify_degree_level(name, html)
    return out


def test_classifies_bachelor_from_name():
    assert _classify("Bachelor of Computer Science") == "Bachelor's"
    assert _classify("B.Sc Computer Science") == "Bachelor's"


def test_classifies_master_from_name():
    assert _classify("Master of Business Administration") == "Master's"
    assert _classify("MBA (Executive)") == "Master's"


def test_classifies_doctorate_from_name():
    assert _classify("Doctor of Philosophy in Engineering") == "Doctorate"
    assert _classify("PhD in Data Science") == "Doctorate"
    assert _classify("Performing Arts Prof Doc") == "Doctorate"
    assert _classify("Educational and Child Psychology Prof Doc") == "Doctorate"
    assert _classify("School of Arts and Creative Industries MPhil PhD") == "Doctorate"


def test_classifies_graduate_certificate():
    # "Graduate Certificate" must beat the looser "certificate" rule that
    # would otherwise classify it as plain "Certificate".
    assert _classify("Graduate Certificate in Marketing") == "Graduate Certificate"
    assert _classify("Postgraduate Diploma in Health Science") == "Graduate Diploma"


def test_classifies_diploma_and_certificate():
    assert _classify("Diploma of Hospitality") == "Diploma"
    assert _classify("Certificate IV in Information Technology") == "Certificate"


def test_falls_back_to_aqf_level_when_name_is_inconclusive():
    # ASA Higher Education and other AU unis use AQF labels — title might
    # just be the program name, with the degree implied by the AQF level.
    out, method, _ = degree_level.classify_degree_level(
        "Information Technology",
        "<p>Course details: AQF Level 7</p>",
    )
    assert out == "Bachelor's"
    assert method == "aqf"


def test_aqf_level_9_is_master():
    out, _, _ = degree_level.classify_degree_level("Foo Bar", "<div>AQF Level 9</div>")
    assert out == "Master's"


def test_returns_none_when_no_signal():
    assert _classify("Some Random Page Title") is None


def test_extract_returns_extraction_result_with_normalized_payload():
    html = "<html><title>Bachelor of Science</title></html>"
    out = asyncio.run(degree_level.extract(html, "https://example.edu/x"))
    r = next(result for result in out if result.field_key == "degree_level")
    assert r.field_key == "degree_level"
    assert r.value == "Bachelor's"
    assert r.normalized == {"degree_level": "Bachelor's"}
    assert r.confidence > 0


def test_extract_uses_passed_course_name_over_generic_title():
    # Pipeline regression: many uni pages have a generic title like
    # "Course details | Example University" while the H1 (already extracted
    # by course_name) carries the degree. The pipeline now passes the
    # extracted name; verify degree_level honors it instead of falling
    # back to the useless <title>.
    html = "<html><title>Course details | Example University</title><body>Apply now.</body></html>"
    out = asyncio.run(
        degree_level.extract(
            html,
            "https://example.edu/x",
            course_name="Master of Cybersecurity",
        )
    )
    result = next(result for result in out if result.field_key == "degree_level")
    assert result.value == "Master's"
    assert result.method == "degree_level:name"


def test_aru_award_panel_owns_degree_not_admissions_or_other_courses():
    html = """<title>Digital Transformation and Public Value - MSc, PG Cert, PG Dip - ARU</title>
    <h1 id="course-page-title">Digital Transformation and Public Value</h1>
    <dl class="utopian-course-options__list"><dt>Award</dt><dd>MSc</dd></dl>
    <p>Entry requirements: Bachelor's degree. Related: Graduate Certificate.</p>"""
    out = asyncio.run(degree_level.extract(
        html, "https://www.aru.ac.uk/study/postgraduate/digital-transformation-and-public-value",
        course_name="Digital Transformation and Public Value",
    ))
    assert [(r.value, r.method) for r in out if r.field_key == "degree_level"] == [
        ("Master's", "degree_level:aru_award")
    ]


def test_aru_course_owned_title_award_when_panel_has_no_award():
    html = """<title>International Business &amp; Artificial Intelligence degree course - BSc (Hons) - ARU</title>
    <h1 id="course-page-title">International Business &amp; Artificial Intelligence</h1>
    <dl class="utopian-course-options__list"><dt>Duration</dt><dd>3 years</dd></dl>"""
    out = asyncio.run(degree_level.extract(
        html, "https://www.aru.ac.uk/study/undergraduate/international-business-and-artificial-intelligence",
        course_name="International Business & Artificial Intelligence",
    ))
    assert [(r.value, r.method) for r in out if r.field_key == "degree_level"] == [
        ("Bachelor's", "degree_level:aru_title_award")
    ]
    assert degree_level._from_aru_course_page(
        html, "https://elsewhere.example/study/undergraduate/artificial-intelligence-and-data-science"
    )[0] is None


def test_aru_ambiguous_awards_do_not_pick_one_from_title_or_panels():
    html = """<title>Data Science - MSc, PGCert - ARU</title>
    <h1 id="course-page-title">Data Science</h1>
    <dl class="utopian-course-options__list"><dt>Award</dt><dd>MSc</dd></dl>
    <dl class="utopian-course-options__list"><dt>Award</dt><dd>PGCert</dd></dl>"""
    assert degree_level._from_aru_course_page(
        html, "https://www.aru.ac.uk/study/postgraduate/data-science"
    )[0] is None
    assert asyncio.run(degree_level.extract(
        html, "https://www.aru.ac.uk/study/postgraduate/data-science",
        course_name="Data Science",
    )) == []
