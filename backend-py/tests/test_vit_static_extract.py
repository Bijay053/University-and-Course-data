"""Unit tests for the VIT static fallback (duration / intake / location).

Each test exercises the structural patterns VIT actually uses on its
course pages: ``<strong>``-labelled paragraphs and ``rbt-list-style-3``
``<ul>``s right after the label.
"""
from __future__ import annotations

import pytest

from app.services.scraper.vit_static_extract import (
    apply_vit_summary_extraction,
    is_vit_url,
)


def test_is_vit_url_matches_vit_subdomains() -> None:
    assert is_vit_url("https://vit.edu.au/courses/mba")
    assert is_vit_url("https://www.vit.edu.au/")
    assert not is_vit_url("https://example.edu.au/courses/mba")
    assert not is_vit_url("not a url")


def test_apply_vit_summary_skips_non_vit_urls() -> None:
    out = apply_vit_summary_extraction("https://swinburne.edu.au/x", "<html></html>", {})
    assert out == {}


def test_extract_intakes_from_label_then_list() -> None:
    """VIT canonical pattern: ``<p>2026 intakes:</p><ul><li>Jan</li>...``"""
    html = """
    <html><body>
      <h1>Bachelor of Business</h1>
      <p class="rbt-label">2026 intakes:</p>
      <ul class="rbt-list-style-3">
        <li><i class="feather-calendar"></i>02-Mar-2026</li>
        <li><i class="feather-calendar"></i>25-May-2026</li>
        <li><i class="feather-calendar"></i>11-Aug-2026</li>
        <li><i class="feather-calendar"></i>20-Sep-2026</li>
        <li><i class="feather-calendar"></i>13-Dec-2026</li>
      </ul>
    </body></html>
    """
    out = apply_vit_summary_extraction("https://vit.edu.au/courses/bbus", html, {})
    assert out["intake_text"] == "March,May,August,September,December"


def test_extract_locations_from_strong_label() -> None:
    """VIT pattern: ``<p><strong>Locations:</strong> Melbourne, Sydney</p>``"""
    html = """
    <html><body>
      <p><strong>Locations:</strong> Melbourne, Sydney, Adelaide, Geelong</p>
    </body></html>
    """
    out = apply_vit_summary_extraction("https://vit.edu.au/courses/bbus", html, {})
    assert out["location_text"] == "Melbourne, Sydney, Adelaide, Geelong"


def test_extract_duration_from_strong_label() -> None:
    """VIT pattern: ``<p><strong>Duration:</strong> Usually a 3 year course...</p>``"""
    html = """
    <html><body>
      <p><strong>Duration:</strong> Usually a 3 year course with full-time intake.</p>
    </body></html>
    """
    out = apply_vit_summary_extraction("https://vit.edu.au/courses/bbus", html, {})
    assert out["duration"] == 3.0
    assert out["duration_term"] == "Year"


def test_extract_all_three_fields_from_realistic_page() -> None:
    html = """
    <html><body>
      <h1>Bachelor of Business</h1>
      <p><strong>Duration:</strong> Usually a 3 year course (full-time).</p>
      <p><strong>Locations:</strong> Melbourne, Sydney, Adelaide, Geelong</p>
      <p>2026 intakes:</p>
      <ul>
        <li>02-Mar-2026</li>
        <li>25-May-2026</li>
        <li>11-Aug-2026</li>
        <li>20-Sep-2026</li>
        <li>13-Dec-2026</li>
      </ul>
    </body></html>
    """
    out = apply_vit_summary_extraction("https://vit.edu.au/courses/bbus", html, {})
    assert out["duration"] == 3.0
    assert out["duration_term"] == "Year"
    assert out["location_text"] == "Melbourne, Sydney, Adelaide, Geelong"
    assert out["intake_text"] == "March,May,August,September,December"


def test_does_not_overwrite_existing_payload_values() -> None:
    """When the payload already has duration / location / intake, the
    extractor should NOT overwrite. Caller is responsible for the
    setdefault-style merge, but the extractor itself short-circuits on
    duration when both ``duration`` and ``duration_term`` are set."""
    html = """
    <html><body>
      <p><strong>Duration:</strong> Usually a 3 year course.</p>
      <p><strong>Locations:</strong> Melbourne, Sydney</p>
      <p>2026 intakes:</p>
      <ul><li>02-Mar-2026</li></ul>
    </body></html>
    """
    payload = {
        "duration": 2.0,
        "duration_term": "Year",
        "location_text": "Existing City",
        "intake_text": "February",
    }
    out = apply_vit_summary_extraction("https://vit.edu.au/courses/bbus", html, payload)
    # duration block short-circuits because both keys are populated.
    assert "duration" not in out
    assert "duration_term" not in out
    # location/intake pre-checked with `not in payload` so they should
    # also be empty in the output.
    assert "location_text" not in out
    assert "intake_text" not in out
