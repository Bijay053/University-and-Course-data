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


def test_classifies_graduate_certificate():
    # "Graduate Certificate" must beat the looser "certificate" rule that
    # would otherwise classify it as plain "Certificate".
    assert _classify("Graduate Certificate in Marketing") == "Graduate Certificate"
    assert _classify("Postgraduate Diploma in Health Science") == "Graduate Certificate"


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
    assert len(out) == 1
    r = out[0]
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
    assert len(out) == 1
    assert out[0].value == "Master's"
    assert out[0].method == "degree_level:name"
