from app.services.scraper.extractors.embedded_english import (
    apply_embedded_english,
    extract_unambiguous_ielts,
)
from app.services.scraper.guards import enforce_source_evidence


def _script(statement: str) -> str:
    return (
        '<html><script type="application/json">'
        '{"heading":"IELTS","summary":"<p>'
        + statement
        + '</p>"}'
        "</script></html>"
    )


def test_extracts_reordered_ielts_statement_from_embedded_json() -> None:
    scores, snippet = extract_unambiguous_ielts(
        _script(
            "Overall Academic IELTS band score of 6.5, "
            "with no band less than 6.0, or equivalent."
        )
    )

    assert scores == {
        "overall": 6.5,
        "listening": 6.0,
        "reading": 6.0,
        "writing": 6.0,
        "speaking": 6.0,
    }
    assert snippet and "Overall Academic IELTS" in snippet


def test_embedded_english_survives_staging_evidence_gate() -> None:
    payload: dict = {}
    evidence: list[dict] = []
    filled = apply_embedded_english(
        payload,
        _script("Academic IELTS overall score 7.0, with no band below 6.5."),
        url="https://example.edu/course/example",
        evidence=evidence,
    )

    staged, dropped = enforce_source_evidence(payload, evidence)

    assert set(filled) == {
        "ielts_overall",
        "ielts_listening",
        "ielts_reading",
        "ielts_writing",
        "ielts_speaking",
    }
    assert staged["ielts_overall"] == 7.0
    assert staged["ielts_reading"] == 6.5
    assert "ielts_overall" not in dropped
    assert all(item["source_url"] for item in evidence)
    assert all(item["snippet"] for item in evidence)


def test_conflicting_embedded_profiles_fail_closed() -> None:
    html = (
        _script("Academic IELTS overall score 6.0, with no band below 6.0.")
        + _script("Academic IELTS overall score 7.0, with no band below 6.5.")
    )

    scores, snippet = extract_unambiguous_ielts(html)

    assert scores == {}
    assert snippet is None


def test_does_not_override_conflicting_existing_overall() -> None:
    payload = {"ielts_overall": 7.0}
    evidence: list[dict] = []

    filled = apply_embedded_english(
        payload,
        _script("Academic IELTS overall score 6.0, with no band below 6.0."),
        url="https://example.edu/course/example",
        evidence=evidence,
    )

    assert filled == []
    assert payload == {"ielts_overall": 7.0}
    assert evidence == []