from app.services.scraper.replay_extraction import _audience_review, _audience_review_from_html


def test_replay_audience_review_keeps_evidence_and_backend_stop_reason():
    evidence = {
        "status": "needs_review",
        "same_panel": True,
        "linked_official": True,
        "issues": ["unbalanced audience evidence"],
        "evidence": [{
            "audience": "international",
            "container": "audience",
            "value": "intl",
            "label": "International March 2027",
            "intake_months": [3],
            "intake_year": 2027,
            "source_url": "https://example.edu/english",
            "source_official": True,
        }],
    }

    review = _audience_review({"audience_evidence": evidence}, {})

    assert review["evidence"] == evidence
    assert review["proposal"] == {
        "status": "needs_review",
        "proposals": [],
        "reason": "unresolved audience evidence",
    }


def test_replay_audience_review_prefers_new_extraction_evidence():
    old = {"audience_evidence": {"status": "needs_review", "issues": ["old"]}}
    new = {"audience_evidence": {"status": "needs_review", "issues": ["current"]}}

    assert _audience_review(old, new)["evidence"]["issues"] == ["current"]


def test_replay_reextracts_audience_evidence_from_stored_html():
    html = """<main><select id="audience">
      <option data-audience="Domestic" value="home">Domestic January 2027</option>
      <option data-audience="International" value="intl"
              data-source="/english">International March 2027</option>
    </select></main>"""

    review = _audience_review_from_html(
        html,
        page_url="https://example.edu/course",
        seed_url="https://example.edu/",
    )

    assert {row["audience"] for row in review["evidence"]["evidence"]} == {
        "domestic", "international",
    }
    assert review["proposal"]["status"] == "accepted"
    assert review["proposal"]["proposals"][0]["english"]["central_page"] == (
        "https://example.edu/english"
    )