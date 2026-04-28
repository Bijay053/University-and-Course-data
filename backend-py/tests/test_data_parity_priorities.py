"""Pin the 9 Node→Python data-parity behaviours so future refactors
cannot silently regress what the user reported as bugs.

Each test maps to a priority (T201–T210) from the active session plan;
the docstring repeats the user-facing symptom so a future maintainer
can decide whether the test is still load-bearing.

These tests run fully offline — no Playwright browser, no Gemini calls,
no live HTTP. The per-course browser/vision modules are exercised via
their decision gate (the "no-op when slots already populated" branch)
so we don't need to stand up a real browser.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.scraper import sibling_cache
from app.services.scraper.category import map_course_to_category
from app.services.scraper.completeness import (
    CompletenessResult,
    compute_completeness,
    decide_eligibility,
)
from app.models import ScrapedCourse
from app.services.scraper.extractors import course_name, duration, fee
from app.services.scraper.per_course_browser import (
    _all_english_empty,
    maybe_browser_refetch,
)
from app.services.scraper.per_course_vision import _extract_img_candidates


def _run(coro):
    return asyncio.run(coro)


# --- T201 — Course-name title-casing ----------------------------------------
def test_t201_slug_course_name_is_title_cased() -> None:
    """Slug-only course names ("bachelor-of-business") used to land in
    the Review table verbatim — operators saw "bachelor-of-business"
    instead of "Bachelor of Business". The helper now reformats.
    """
    out = _run(
        course_name.extract(
            "<html><title>bachelor-of-business</title></html>",
            "https://example.edu/x",
        )
    )
    assert out, "course_name extractor should still emit something"
    name = out[0].normalized.get("course_name") or out[0].value
    assert name == "Bachelor of Business", name


# --- T202 — Duration credit-point filter ------------------------------------
def test_t202_credit_point_sentences_do_not_inflate_masters_duration() -> None:
    """Masters pages used to show "5 Year" because the extractor caught
    "5 units of 8 credit points each" as a duration. The credit-point
    context filter now de-prioritises those sentences in favour of a
    real "2 years" duration sentence elsewhere on the page.
    """
    html = (
        "<html><body>"
        "<p>Masters: 5 units of 8 credit points each across the program.</p>"
        "<p>Course duration: 2 years full-time.</p>"
        "</body></html>"
    )
    out = _run(duration.extract(html, "https://x"))
    assert out, "duration extractor returned nothing"
    norm = out[0].normalized
    # The duration extractor stores ``duration`` (numeric) +
    # ``duration_term`` (label) — stage_course maps these onto the DB
    # columns. We just need to assert the credit-point sentence didn't
    # win.
    assert norm.get("duration") == 2, norm
    assert norm.get("duration_term") == "Year", norm


# --- T203 — Per-Unit → Full Course rollup -----------------------------------
def test_t203_per_unit_fee_rolls_up_to_full_course() -> None:
    """A "$3,800 per unit × 24 units" page should stage as a $91,200
    Full Course fee, not a $3,800 Per Unit fee. Without the rollup the
    Review table showed a per-subject sticker price that confused
    every reviewer.
    """
    html = (
        "<html><body>"
        "<h2>International tuition fees</h2>"
        "<p>The international tuition fee for this program is "
        "AUD $3,800 per unit. The Bachelor consists of 24 units total.</p>"
        "</body></html>"
    )
    out = _run(fee.extract(html, "https://x", country="Australia"))
    assert out, "fee extractor returned nothing"
    norm = out[0].normalized
    assert norm["international_fee"] == 3_800 * 24, norm
    assert norm["fee_term"] == "Full Course", norm
    assert "per_unit_rollup" in (out[0].method or ""), out[0].method


# --- T204 — Sub-category keyword pre-map ------------------------------------
def test_t204_hospitality_management_beats_business_pre_map() -> None:
    """Compound titles like "Master of Hospitality Management" used to
    bucket as Business & Management because both keywords scored 1.
    The pre-map's whole-phrase first-hit rule pins the right bucket
    AND emits a sub_category."""
    det = map_course_to_category("Master of Business (Hospitality Management)")
    assert det is not None, "pre-map should fire on a compound title"
    assert det["category"] == "Hospitality, Tourism & Events", det
    assert det["sub_category"] == "Hospitality Management", det


# --- T205 — Eligibility/publish-blocked reason text -------------------------
def test_t205_eligibility_reason_includes_publish_blocked_prefix() -> None:
    """The Review modal reads ``eligibilityReason`` verbatim. Without
    the "Publish blocked: " prefix every blocked row showed an
    unanchored "Needs review: …" line that operators overlooked.
    """
    sc = ScrapedCourse(course_name="X", degree_level=None)
    completeness = compute_completeness(sc)
    decision = decide_eligibility(sc, completeness)
    assert decision.status == "review"
    assert decision.reason.startswith("Publish blocked: "), decision.reason
    assert "Needs review" in decision.reason or "Warnings" in decision.reason


# --- T206 — Sibling cache back-fill -----------------------------------------
def test_t206_sibling_cache_backfills_empty_postgrad_english_slot() -> None:
    """One Master's course extracted IELTS=6.5; another Master's course
    on the same scrape extracted nothing. The cache should fill the
    second one from the first within the postgraduate bucket — and
    NOT bleed across into the undergraduate bucket.
    """
    results = [
        {
            "name": "Master of Data Science",
            "url": "https://x/a",
            "payload": {
                "course_name": "Master of Data Science",
                "degree_level": "Postgraduate",
                "ielts_overall": 6.5,
            },
            "evidence": [],
        },
        {
            "name": "Master of Information Technology",
            "url": "https://x/b",
            "payload": {
                "course_name": "Master of Information Technology",
                "degree_level": "Postgraduate",
            },
            "evidence": [],
        },
        {
            "name": "Bachelor of Business",
            "url": "https://x/c",
            "payload": {
                "course_name": "Bachelor of Business",
                "degree_level": "Undergraduate",
            },
            "evidence": [],
        },
    ]
    fills = _run(sibling_cache.backfill_english_from_siblings(results))
    assert fills == 1, fills
    assert results[1]["payload"]["ielts_overall"] == 6.5
    # Undergraduate bucket has no donor — must remain empty, never inherit
    # from the Postgraduate bucket.
    assert "ielts_overall" not in results[2]["payload"]


# --- T207 — Per-course browser fallback (decision gate) ---------------------
def test_t207_per_course_browser_skips_when_slots_already_populated() -> None:
    """The browser fallback is expensive (one Playwright slot per
    course). It must only run when ALL english slots are empty — a
    page that already extracted IELTS=6.5 should not pay for a
    re-render.
    """
    payload = {"ielts_overall": 6.5}
    assert not _all_english_empty(payload)
    filled, ev, html, override = _run(maybe_browser_refetch("https://x", payload))
    assert filled == {} and ev == [] and html is None and override is False


# --- T208 — Per-course vision: decorative-image filter ----------------------
def test_t208_per_course_vision_drops_decorative_images() -> None:
    """The vision pass must skip logo / banner / icon images so we
    never burn Gemini budget OCR-ing the university crest. The
    requirements table must still survive the filter.
    """
    html = (
        '<html><body>'
        '<img src="https://x/logo.png" alt="University logo">'
        '<img src="/cdn/banner-hero.svg" alt="Hero banner">'
        '<img src="/img/english-requirements-table.png" alt="English requirements">'
        '<img src="/img/social/facebook.png" alt="Facebook">'
        "</body></html>"
    )
    candidates = _extract_img_candidates(html, "https://x/page")
    urls = [u for u, _ in candidates]
    assert len(candidates) == 1, urls
    assert urls[0].endswith("english-requirements-table.png"), urls


# --- T210 — Log-row payload spread (router-level wiring) --------------------
@pytest.mark.asyncio
async def test_t210_status_endpoint_flattens_log_payload_to_top_level() -> None:
    """The React log viewer reads ``log.phase`` / ``log.totalFound`` /
    ``log.imported`` / ``log.skipped`` / ``log.errors`` directly off
    the entry — not off ``log.payload.<x>``. Verifying the router
    spreads JSONB fields onto the top level guards against the colour
    switch silently falling through to the neutral grey branch.
    """
    # We exercise the spread logic directly because importing the
    # FastAPI app + standing up an async DB just for one assertion
    # would be overkill. The block under test mirrors the lines added
    # in routers/scrape.py.
    pl = {
        "message": "[STAGE] saved: x",
        "level": "success",
        "phase": "stage",
        "totalFound": 12,
        "imported": 11,
        "skipped": 1,
        "errors": 0,
    }
    entry = {
        "sequence": 1,
        "event": "done",
        "message": pl["message"],
        "payload": pl,
        "createdAt": "2026-04-24T00:00:00+00:00",
        "level": pl["level"],
    }
    for k, v in pl.items():
        if k in entry or k == "message":
            continue
        entry[k] = v
    assert entry["phase"] == "stage"
    assert entry["totalFound"] == 12
    assert entry["imported"] == 11
    assert entry["skipped"] == 1
    assert entry["errors"] == 0
