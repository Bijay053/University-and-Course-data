"""Regression tests for Federation's authoritative JSON course metadata."""
from __future__ import annotations

import inspect

from app.services.scraper.extractors.federation_json import (
    apply_overrides,
    extract_intake_months,
    extract_locations,
)
from app.services.scraper.pipelines.single_course import extract_course


def test_federation_uses_course_detail_start_dates_block() -> None:
    html = r'''
    <script>
      {
        "heading": "Start dates",
        "summary": "05 October 2026<br>25 January 2027<br>01 March 2027<br>19 April 2027<br>12 July 2027<br>26 July 2027<br>04 October 2027"
      }
      {
        "heading": "Start dates",
        "summary": "20 July 2026<br>01 March 2027<br>26 July 2027"
      }
    </script>
    '''

    months, summary = extract_intake_months(html)

    assert months == ["March", "July"]
    assert summary == "20 July 2026<br>01 March 2027<br>26 July 2027"


def test_federation_single_start_dates_block_is_unchanged() -> None:
    html = r'''
    {
      "heading": "Start dates",
      "summary": "01 March 2027<br>26 July 2027"
    }
    '''

    months, summary = extract_intake_months(html)

    assert months == ["March", "July"]
    assert summary == "01 March 2027<br>26 July 2027"


def test_federation_uses_course_detail_locations_block() -> None:
    html = r'''
    {
      "heading": "Locations",
      "summary": "Berwick (on campus)<br>Gippsland (on campus)<br>Mt Helen (on campus)"
    }
    {
      "heading": "Locations",
      "summary": "Berwick (on campus)<br>Mt Helen (on campus)"
    }
    '''

    campuses, online_only, summary = extract_locations(html)

    assert campuses == ["Berwick", "Mt Helen"]
    assert online_only is False
    assert summary == "Berwick (on campus)<br>Mt Helen (on campus)"


def test_federation_late_override_cleans_name_without_config_context() -> None:
    html = r'''
    {
      "heading": "Bachelor of Psychological Science",
      "summary": "Course"
    }
    {
      "heading": "Start dates",
      "summary": "05 October 2026<br>25 January 2027"
    }
    {
      "heading": "Start dates",
      "summary": "20 July 2026<br>01 March 2027<br>26 July 2027"
    }
    '''
    payload = {
        "course_name": (
            "Bachelor of Psychological Science | Federation University"
        ),
        "intake_months": ["October", "January"],
    }

    apply_overrides(
        payload,
        html,
        url=(
            "https://www.federation.edu.au/courses/"
            "dhy5-bachelor-of-psychological-science/"
        ),
    )

    assert payload["course_name"] == "Bachelor of Psychological Science"
    assert payload["intake_months"] == ["March", "July"]


def test_federation_authority_is_not_nested_under_ai_fallback() -> None:
    """Complete-looking aggregate fields must not suppress Federation JSON."""
    source_lines = inspect.getsource(extract_course).splitlines()
    authority_gate = next(
        line
        for line in source_lines
        if line.strip() == "if _fed_json.is_federation_host(url):"
    )

    # Function-body scope is four spaces. Eight spaces would place the
    # authority back inside ``if use_ai_fallback:``, reproducing the DHY5 bug.
    assert authority_gate.startswith("    if ")
    assert not authority_gate.startswith("        if ")