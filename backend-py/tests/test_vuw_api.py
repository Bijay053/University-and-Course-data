"""Unit tests for vuw_api.py — VUW JSON API provider.

Focus: the enforce_source_evidence gate that requires both source_url AND
snippet in evidence for critical fields (international_fee, ielts_overall,
study_mode). Verifies the bug fix where missing snippets caused every
critical field to be silently nulled out after staging.
"""
from __future__ import annotations

import pytest

from app.services.scraper.vuw_api import _ev, _fee, _map_item
from app.services.scraper.config.schema import VuwApiConfig
from app.services.scraper.guards import enforce_source_evidence


# ── fixtures ──────────────────────────────────────────────────────────────────

def _cfg(**overrides) -> VuwApiConfig:
    defaults = dict(
        enabled=True,
        currency="NZD",
        endpoints=["ug-programmes"],
        base_url="https://www.wgtn.ac.nz",
    )
    defaults.update(overrides)
    return VuwApiConfig(**defaults)


_UG_ITEM = {
    "name": "Bachelor of Architectural Studies",
    "url": "https://www.wgtn.ac.nz/explore/undergraduate-programmes/bachelor-of-architectural-studies/overview",
    "internationalFeeTotal": 44450,
    "internationalFeeTerm": "per 120 points",
    "partTimeQual": False,
    "fullTimeQual": True,
    "durationDescription": "3 years full-time",
    "description": "A design-based architecture programme.",
}

_PG_ITEM = {
    "name": "Master of Architecture",
    "url": "https://www.wgtn.ac.nz/explore/postgraduate-programmes/master-of-architecture/overview",
    "internationalFeeTotal": 55000,
    "internationalFeeTerm": "per 120 points",
    "partTimeQual": False,
    "fullTimeQual": True,
    "durationDescription": "2 years full-time",
    "description": "Advanced architectural design study.",
}

_NO_FEE_ITEM = {
    "name": "Bachelor of Arts with Honours",
    "url": "https://www.wgtn.ac.nz/explore/undergraduate-programmes/bachelor-of-arts-with-honours/overview",
    "internationalFeeTotal": None,
    "internationalFeeTerm": None,
}


# ── _ev helper ────────────────────────────────────────────────────────────────

def test_ev_includes_snippet_field():
    ev = _ev("international_fee", 44450.0, "vuw_api:internationalFeeTotal",
             "https://www.wgtn.ac.nz/...", snippet="NZ$44,450 per 120 points")
    assert ev["snippet"] == "NZ$44,450 per 120 points"
    assert ev["field_key"] == "international_fee"
    assert ev["source_url"] == "https://www.wgtn.ac.nz/..."


def test_ev_snippet_defaults_to_empty_string():
    ev = _ev("course_name", "Bachelor of Arts", "vuw_api:name", "https://x")
    assert "snippet" in ev
    assert ev["snippet"] == ""


# ── _fee helper ───────────────────────────────────────────────────────────────

def test_fee_per_120_points_is_annual():
    amt, term, _ = _fee({"internationalFeeTotal": 40500, "internationalFeeTerm": "per 120 points"})
    assert amt == pytest.approx(40500.0)
    assert term == "Year"


def test_fee_full_programme_is_total():
    amt, term, _ = _fee({"internationalFeeTotal": 37400, "internationalFeeTerm": "full programme"})
    assert amt == pytest.approx(37400.0)
    assert term == "Total"


def test_fee_none_when_missing():
    amt, term, _ = _fee({"internationalFeeTotal": None})
    assert amt is None


def test_fee_zero_treated_as_none():
    amt, _, _ = _fee({"internationalFeeTotal": 0})
    assert amt is None


# ── _map_item — enforce_source_evidence compatibility ─────────────────────────

def test_ug_fee_survives_enforce_source_evidence():
    """international_fee must pass enforce_source_evidence (needs snippet)."""
    link = _map_item(_UG_ITEM, _cfg())
    payload = link["searchstax_result"]["payload"]
    evidence = link["searchstax_result"]["evidence"]

    assert payload["international_fee"] == pytest.approx(44450.0)

    cleaned, dropped = enforce_source_evidence(payload, evidence)
    assert "international_fee" not in dropped
    assert cleaned["international_fee"] == pytest.approx(44450.0)


def test_ug_ielts_default_survives_enforce_source_evidence():
    """ielts_overall (degree-level default) must pass enforce_source_evidence."""
    link = _map_item(_UG_ITEM, _cfg())
    payload = link["searchstax_result"]["payload"]
    evidence = link["searchstax_result"]["evidence"]

    assert payload["ielts_overall"] == pytest.approx(6.0)

    cleaned, dropped = enforce_source_evidence(payload, evidence)
    assert "ielts_overall" not in dropped
    assert cleaned["ielts_overall"] == pytest.approx(6.0)


def test_pg_ielts_default_is_65():
    link = _map_item(_PG_ITEM, _cfg())
    payload = link["searchstax_result"]["payload"]
    evidence = link["searchstax_result"]["evidence"]

    assert payload["ielts_overall"] == pytest.approx(6.5)

    cleaned, dropped = enforce_source_evidence(payload, evidence)
    assert "ielts_overall" not in dropped
    assert cleaned["ielts_overall"] == pytest.approx(6.5)


def test_no_critical_fields_dropped_for_ug():
    link = _map_item(_UG_ITEM, _cfg())
    payload = link["searchstax_result"]["payload"]
    evidence = link["searchstax_result"]["evidence"]
    _, dropped = enforce_source_evidence(payload, evidence)
    assert dropped == [], f"Unexpected dropped fields: {dropped}"


def test_no_critical_fields_dropped_for_pg():
    link = _map_item(_PG_ITEM, _cfg())
    payload = link["searchstax_result"]["payload"]
    evidence = link["searchstax_result"]["evidence"]
    _, dropped = enforce_source_evidence(payload, evidence)
    assert dropped == [], f"Unexpected dropped fields: {dropped}"


def test_fee_snippet_contains_nzd_and_term():
    link = _map_item(_UG_ITEM, _cfg())
    evidence = link["searchstax_result"]["evidence"]
    fee_ev = next(e for e in evidence if e["field_key"] == "international_fee")
    assert "NZ$" in fee_ev["snippet"]
    assert "per 120 points" in fee_ev["snippet"]


def test_ielts_snippet_mentions_ug_requirement():
    link = _map_item(_UG_ITEM, _cfg())
    evidence = link["searchstax_result"]["evidence"]
    ielts_ev = next(e for e in evidence if e["field_key"] == "ielts_overall")
    assert "6.0" in ielts_ev["snippet"]
    assert ielts_ev["source_url"]  # non-empty


def test_ielts_snippet_mentions_pg_requirement():
    link = _map_item(_PG_ITEM, _cfg())
    evidence = link["searchstax_result"]["evidence"]
    ielts_ev = next(e for e in evidence if e["field_key"] == "ielts_overall")
    assert "6.5" in ielts_ev["snippet"]


# ── course without fee ────────────────────────────────────────────────────────

def test_no_fee_item_has_no_international_fee():
    link = _map_item(_NO_FEE_ITEM, _cfg())
    payload = link["searchstax_result"]["payload"]
    assert payload.get("international_fee") is None


def test_no_fee_item_ielts_still_set():
    """IELTS default is always added regardless of fee presence."""
    link = _map_item(_NO_FEE_ITEM, _cfg())
    payload = link["searchstax_result"]["payload"]
    evidence = link["searchstax_result"]["evidence"]
    assert payload.get("ielts_overall") is not None

    cleaned, dropped = enforce_source_evidence(payload, evidence)
    assert "ielts_overall" not in dropped


# ── searchstax_result shape ───────────────────────────────────────────────────

def test_map_item_returns_searchstax_result_shape():
    link = _map_item(_UG_ITEM, _cfg())
    sr = link["searchstax_result"]
    assert "name" in sr
    assert "url" in sr
    assert "payload" in sr
    assert "evidence" in sr


def test_map_item_url_appends_international_true():
    link = _map_item(_UG_ITEM, _cfg())
    assert link["url"].endswith("?international=true")
    assert link["searchstax_result"]["payload"]["course_website"].endswith("?international=true")


def test_map_item_returns_none_for_missing_name():
    item = {"url": "https://www.wgtn.ac.nz/explore/ug/something/overview"}
    assert _map_item(item, _cfg()) is None


def test_map_item_returns_none_for_missing_url():
    item = {"name": "Bachelor of Something"}
    assert _map_item(item, _cfg()) is None
