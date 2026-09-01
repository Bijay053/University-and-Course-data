"""Regression tests for Federation's authoritative JSON course metadata."""
from __future__ import annotations

from app.services.scraper.extractors.federation_json import extract_intake_months


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