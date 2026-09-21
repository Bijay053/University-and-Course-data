from __future__ import annotations

import json

import pytest

from app.services.scraper.cccu_embedded_facts import extract_cccu_embedded_facts
from app.services.scraper.extractors import english_test, fee

URL = (
    "https://www.canterbury.ac.uk/study-here/courses/"
    "animal-science-with-foundation-year?year=september-2026&modeRef=0"
)


def _page() -> str:
    state = {
        "routing": {
            "entry": {
                "entryYears": [
                    {
                        "entryTitle": (
                            "Course main content - Animal Science - "
                            "Foundation Year - Undergraduate - 2026/27"
                        ),
                        "entryYear": {"entryTitle": "September 2026"},
                        "courseFees": {
                            "entryTitle": (
                                "Course fees - B - Standard UG with Foundation "
                                "Year - higher amount - 2026/27"
                            ),
                            "feesTable": """
                                <table>
                                  <tr><th></th><th>UK</th><th>Overseas</th></tr>
                                  <tr><td>Full-time - Foundation Year 0</td>
                                      <td>£9,790</td><td>£17,000</td></tr>
                                  <tr><td>Full-time - years 1-3</td>
                                      <td>£10,050</td><td>£17,000</td></tr>
                                </table>
                            """,
                        },
                        "internationalAside": [
                            {
                                "content": """
                                  <table>
                                    <tr><td>Course type</td><td>IELTS</td></tr>
                                    <tr>
                                      <td>Standard undergraduate and
                                          postgraduate programmes</td>
                                      <td>6.0 overall with no element below 5.5</td>
                                    </tr>
                                  </table>
                                """
                            }
                        ],
                    }
                ]
            }
        }
    }
    return (
        "<html><script>window.versionStatus = \"published\"; "
        f"window.REDUX_DATA = {json.dumps(state)}</script></html>"
    )


def test_selected_current_year_facts_come_from_owned_embedded_state():
    facts = extract_cccu_embedded_facts(_page(), URL)
    assert facts == {
        "fee": {
            "international_fee": 17_000.0,
            "currency": "GBP",
            "fee_term": "Annual",
            "fee_year": 2026,
            "snippet": "Full-time - Foundation Year 0 | £9,790 | £17,000",
        },
        "english": {
            "ielts_overall": 6.0,
            "ielts_listening": 5.5,
            "ielts_reading": 5.5,
            "ielts_writing": 5.5,
            "ielts_speaking": 5.5,
        },
    }


@pytest.mark.asyncio
async def test_cccu_extractors_use_embedded_fee_and_ielts():
    fee_results = await fee.extract(_page(), URL, country="United Kingdom")
    english_results = await english_test.extract(_page(), URL)

    assert fee_results[0].normalized == {
        "international_fee": 17_000.0,
        "currency": "GBP",
        "fee_term": "Annual",
        "fee_year": 2026,
    }
    assert fee_results[0].method == "fee.cccu_embedded_course_year"
    assert english_results[0].normalized["ielts_overall"] == 6.0
    assert english_results[0].normalized["ielts_writing"] == 5.5
    assert english_results[0].method == "english.cccu_embedded_requirements"