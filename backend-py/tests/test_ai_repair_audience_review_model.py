from app.services.scraper.ai_repair_agent import (
    _normalize_repair_audit_evidence,
    _repair_audience_reviews,
)


def _probe():
    return {
        "samples": [{
            "url": "https://example.edu/course",
            "title": "Example course",
            "audience_evidence": {
                "status": "accepted",
                "same_panel": True,
                "linked_official": True,
                "issues": [],
                "evidence": [{
                    "audience": "international",
                    "container": "audience",
                    "value": "intl",
                    "source_url": "https://example.edu/english",
                    "intake_months": [3],
                }],
            },
        }],
    }


def test_repair_audience_review_pairs_evidence_with_backend_verdict():
    reviews = _repair_audience_reviews(_probe())

    assert reviews[0]["url"] == "https://example.edu/course"
    assert reviews[0]["evidence"]["evidence"][0]["value"] == "intl"
    assert reviews[0]["proposal"]["status"] == "needs_review"
    assert reviews[0]["proposal"]["reason"] == "unbalanced audience evidence"


def test_old_audit_is_hydrated_to_stable_audience_review_model():
    old = {
        "live_probe": _probe(),
        "audience_evidence": [_probe()["samples"][0]["audience_evidence"]],
    }

    hydrated = _normalize_repair_audit_evidence(old)

    assert hydrated["audience_reviews"][0]["title"] == "Example course"
    assert hydrated["audience_reviews"][0]["proposal"]["reason"] == (
        "unbalanced audience evidence"
    )