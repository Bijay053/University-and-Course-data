"""Source-template regression tests for UEL non-option row classification."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.services.scraper.extractors.uel_variants import parse_uel_variants


FIXTURES = Path(__file__).parent / "fixtures/uel_option_templates"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_source_fixture_hashes_and_capture_provenance_are_pinned():
    provenance = json.loads((FIXTURES / "provenance.json").read_text(encoding="utf-8"))

    assert provenance["run"] == "20260918T034045Z_b9e9ee"
    for name, record in provenance["fixtures"].items():
        body = (FIXTURES / name).read_bytes()
        assert hashlib.sha256(body).hexdigest() == record["fixture_sha256"]
        assert record["capture_file"] == f'{record["capture_sha256"]}.html'
        assert record["source_url"].startswith("https://www.uel.ac.uk/")
        assert "/courses/" in record["source_url"]


def test_status_stub_preserves_international_separately_from_complete_attendance():
    name = "physiotherapy_status_stub.source.html"
    provenance = json.loads((FIXTURES / "provenance.json").read_text())["fixtures"][name]

    variants = {
        variant.key: variant
        for variant in parse_uel_variants(_fixture(name), provenance["source_url"])
    }
    degree = variants["degree"]
    foundation = variants["degree-via-foundation-year"]

    assert degree.international is True
    assert degree.full_time is None
    assert "Home Applicant" not in degree.html
    assert "International applications will open" not in degree.html
    # International status is eligibility evidence, not permission to relabel
    # the selected Home fee as an international fee.
    assert "Annual tuition fee (GBP)" not in degree.html
    assert "Study load" not in degree.html
    assert "Duration" not in degree.html
    assert "Intakes" not in degree.html
    assert 'data-uel-requirements="matched"' in degree.html
    assert 'id="entry-req-details-1"' in degree.html

    assert foundation.international is False
    assert foundation.full_time is True
    assert "Domestic applicants only" in foundation.html
    assert 'data-uel-requirements="matched"' in foundation.html
    assert 'id="entry-req-details-2"' in foundation.html


def test_linked_course_card_is_excluded_without_losing_valid_sibling_routes():
    name = "psychology_linked_course_card.source.html"
    provenance = json.loads((FIXTURES / "provenance.json").read_text())["fixtures"][name]

    degree, foundation = parse_uel_variants(_fixture(name), provenance["source_url"])

    assert [degree.key, foundation.key] == ["degree", "degree-with-foundation-year"]
    assert all(variant.international and variant.full_time for variant in (degree, foundation))
    assert all("Distance Learning BSc" not in variant.html for variant in (degree, foundation))
    assert 'id="entry-req-details-1"' in degree.html
    assert 'id="entry-req-details-2"' not in degree.html
    assert 'id="entry-req-details-2"' in foundation.html
    assert 'id="entry-req-details-1"' not in foundation.html


def test_missing_route_owned_requirements_remain_explicitly_missing():
    name = "psychology_linked_course_card.source.html"
    provenance = json.loads((FIXTURES / "provenance.json").read_text())["fixtures"][name]
    html = _fixture(name).replace(
        '<div class="entry-requirements-details-screen" '
        'data-label="Degree with foundation year"',
        '<div class="entry-requirements-details-screen" data-label="Unknown route"',
    )

    foundation = next(
        variant
        for variant in parse_uel_variants(html, provenance["source_url"])
        if variant.key == "degree-with-foundation-year"
    )
    assert 'data-uel-requirements="missing"' in foundation.html
    assert "IELTS 6.0" not in foundation.html


@pytest.mark.parametrize(
    ("fixture_name", "old", "new"),
    [
        (
            "physiotherapy_status_stub.source.html",
            "International applications will open later this year",
            "Contact us for details",
        ),
        (
            "physiotherapy_status_stub.source.html",
            "International applications will open later this year",
            "International applications are currently closed",
        ),
        (
            "physiotherapy_status_stub.source.html",
            "<label>Fees:</label></span>",
            "<label>Fees:</label>£1</span>",
        ),
        (
            "physiotherapy_status_stub.source.html",
            "</span>\n    </div>\n  </div>\n</div>\n<dialog",
            "</span><span class=\"unknown-field\">extra</span>\n    </div>\n  </div>\n</div>\n<dialog",
        ),
        (
            "physiotherapy_status_stub.source.html",
            "<span class=\"course-details message-type\">",
            "<a href=\"/apply\">Apply</a><span class=\"course-details message-type\">",
        ),
        (
            "psychology_linked_course_card.source.html",
            "/undergraduate/courses/bsc-hons-psychology-distance-learning",
            "https://example.invalid/something",
        ),
        (
            "psychology_linked_course_card.source.html",
            "/undergraduate/courses/bsc-hons-psychology-distance-learning",
            "/undergraduate/courses/bsc-hons-psychology",
        ),
        (
            "psychology_linked_course_card.source.html",
            "</a>\n    </div>",
            "</a><a class=\"distance-link\" href=\"/undergraduate/courses/other\">Other</a>\n    </div>",
        ),
        (
            "psychology_linked_course_card.source.html",
            "</a>\n    </div>",
            "</a><span class=\"unknown-field\">extra</span>\n    </div>",
        ),
    ],
)
def test_unknown_partial_rows_still_fail_closed(fixture_name, old, new):
    provenance = json.loads((FIXTURES / "provenance.json").read_text())["fixtures"][
        fixture_name
    ]
    malformed = _fixture(fixture_name).replace(old, new, 1)

    with pytest.raises(ValueError, match="unknown"):
        parse_uel_variants(malformed, provenance["source_url"])