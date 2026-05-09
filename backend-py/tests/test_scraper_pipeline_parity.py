"""Pipeline-parity regression tests for T201-T206.

Each test pins one Node-vs-Python parity feature so a regression on the
relevant module fails loudly with a sensible diff. Kept thin and pure
(no DB, no network) — heavyweight integration coverage lives elsewhere.
"""
from __future__ import annotations

import asyncio

import pytest

from app.models import ScrapedCourse
from app.services.scraper.category import (
    classify_category,
    map_course_to_category,
)
from app.services.scraper.completeness import (
    compute_completeness,
    decide_eligibility,
)
from app.services.scraper.extractors import course_name as course_name_mod
from app.services.scraper.extractors import duration as duration_mod
from app.services.scraper.extractors import fee as fee_mod
from app.services.scraper.sibling_cache import backfill_english_from_siblings


# ─── T201: course-name slug title-casing ─────────────────────────────────
def test_t201_slug_is_title_cased_with_lowercase_prepositions():
    """A URL-style slug ``bachelor-of-business`` must surface as
    ``Bachelor of Business`` — capitalised content words, lower-case
    prepositions ("of"). Anything that ends with ``-of-X-Y`` was a recurring
    bug in prod where the H1 fell through to the URL slug and the Review
    table rendered ``Bachelor-of-business``.
    """
    html = (
        "<html><head><title>ignore</title></head>"
        "<body><h1>bachelor-of-business-administration</h1></body></html>"
    )
    [res] = asyncio.run(course_name_mod.extract(html, "https://x"))
    assert res.value == "Bachelor of Business Administration"
    # Stem also covered (more hyphens, plain "of"):
    html2 = "<html><body><h1>master-of-data-science</h1></body></html>"
    [res2] = asyncio.run(course_name_mod.extract(html2, "https://x"))
    assert res2.value == "Master of Data Science"


def test_t201_legitimate_compound_words_are_not_mangled():
    """Single-hyphen survivors like "co-op" / "part-time" must NOT trigger
    the slug branch — that branch only fires for two-or-more-hyphen tokens.
    """
    html = "<html><body><h1>Bachelor of Co-op Engineering</h1></body></html>"
    [res] = asyncio.run(course_name_mod.extract(html, "https://x"))
    assert res.value == "Bachelor of Co-op Engineering"


# ─── T202: duration term suffix + Masters credit-points fix ─────────────
def test_t202_year_duration_emits_year_term():
    html = "<html><body><p>Course duration: 3 years full-time.</p></body></html>"
    [res] = asyncio.run(duration_mod.extract(html, "https://x"))
    assert res.value == 3.0
    assert res.normalized["duration_term"] == "Year"


def test_t202_month_duration_emits_month_term():
    html = "<html><body><p>Duration: 18 months full-time</p></body></html>"
    [res] = asyncio.run(duration_mod.extract(html, "https://x"))
    assert res.value == 18.0
    assert res.normalized["duration_term"] == "Month"


def test_t202_masters_credit_points_does_not_become_5_year_program():
    """The exact bug the user reported — Masters showing 5 instead of 2
    because credit-points talk leaked into the duration regex. The
    extractor must demote credit-point sentences so the real "2 years"
    sentence wins.
    """
    html = (
        "<html><body>"
        "<p>The Master of Information Technology comprises 5 units of 8 "
        "credit points each, taken across 2 years full-time.</p>"
        "</body></html>"
    )
    [res] = asyncio.run(duration_mod.extract(html, "https://x"))
    assert res.value == 2.0, (
        f"Expected 2 years (the 'across 2 years' sentence) but got {res.value} "
        f"— credit-points talk leaked into the duration extractor again."
    )
    assert res.normalized["duration_term"] == "Year"


# ─── T203: per-unit fee → full-course multiplier ────────────────────────
def test_t203_per_unit_fee_rolls_up_to_full_course():
    """A Master of IT page that quotes ``$3,500 per unit`` and ``24 units``
    should stage as a Full Course fee of $84,000 (3,500 × 24), not a
    misleading $3,500 sticker. Mirrors Node's per-unit rollup.
    """
    html = (
        "<html><body>"
        "<h2>International tuition fee</h2>"
        "<p>The Master of IT is offered at A$3,500 per unit for "
        "international students. The course consists of 24 units total.</p>"
        "</body></html>"
    )
    [res] = asyncio.run(fee_mod.extract(html, "https://x"))
    assert res.normalized["fee_term"] == "Full Course"
    assert res.value == 84_000  # 3,500 * 24
    assert "per_unit_rollup" in (res.method or "")


def test_t203_no_unit_count_means_no_rollup():
    """If the page only quotes the per-unit rate without disclosing total
    units, we leave it as Per Unit rather than guess a multiplier.
    """
    html = (
        "<html><body>"
        "<p>Tuition: A$3,500 per unit for international students.</p>"
        "</body></html>"
    )
    [res] = asyncio.run(fee_mod.extract(html, "https://x"))
    assert res.normalized["fee_term"] == "Per Unit"
    assert res.value == 3_500


# ─── T204: keyword pre-map for AI sub-classification ────────────────────
def test_t204_hospitality_management_pinned_to_correct_bucket():
    """Without the pre-map, ``Master of Hospitality Management`` was
    bucketed as Business & Management because of the bare ``management``
    token. The pre-map must pin it to Hospitality / Hospitality Management.
    """
    out = map_course_to_category("Master of Hospitality Management")
    assert out is not None
    assert out["category"] == "Hospitality, Tourism & Events"
    assert out["sub_category"] == "Hospitality Management"


def test_t204_premap_runs_before_generic_classifier():
    """The pre-map result must align with — or be more specific than —
    the generic classifier for canonical examples. Sanity check that
    both layers agree on Hospitality so the pipeline's
    "premap first, fallback to classify_category" order doesn't downgrade
    the answer for well-known titles.
    """
    name = "Diploma of Hospitality Management"
    pre = map_course_to_category(name)
    cls = classify_category(name)
    assert pre is not None
    assert pre["category"] == cls == "Hospitality, Tourism & Events"


def test_t204_unknown_name_returns_none():
    assert map_course_to_category("Bachelor of Existential Vibes") is None


# ─── T205: eligibility reason follows Node format ───────────────────────
def _bare_course() -> ScrapedCourse:
    """Minimum-fields course: passes basic instantiation but missing
    most review slots so completeness is low and several missing-field
    warnings fire.
    """
    return ScrapedCourse(
        scrape_job_id="j",
        university_id=1,
        course_name="Bachelor of Test",
        degree_level="Bachelor's",
        ielts_overall=6.5,
    )


def test_t205_reason_starts_with_publish_blocked_no_needs_review_prefix():
    """Reason format must be ``"Publish blocked: <blockers> | Missing: ...
    | Warnings: ..."`` — no ``"Needs review:"`` interstitial prefix
    (that was a Python-only divergence; Node never emitted it).
    """
    sc = _bare_course()
    sc.degree_level = None  # blocker
    comp = compute_completeness(sc)
    decision = decide_eligibility(sc, comp)
    assert decision.status == "review"
    assert decision.reason.startswith("Publish blocked: degreeLevel")
    assert "Needs review" not in decision.reason


def test_t205_reason_includes_missing_section_separately_from_blockers():
    """``Missing:`` enumerates fields that completeness flagged but
    weren't already surfaced in the blockers section. With only
    course_name + degree_level + IELTS set, plenty of canonical fields
    fall into Missing.
    """
    sc = _bare_course()
    sc.degree_level = None
    comp = compute_completeness(sc)
    decision = decide_eligibility(sc, comp)
    assert " | Missing: " in decision.reason
    # courseName isn't missing here (we set "Bachelor of Test"), so the
    # de-duplication logic shouldn't drop it from Missing for spurious
    # reasons.
    assert "category" in decision.reason
    # Sections appear in spec order (Publish blocked → Validation →
    # Missing → Warnings). Validation is currently empty so we only
    # check that Missing appears before Warnings.
    miss_idx = decision.reason.index("Missing:")
    warn_idx = decision.reason.index("Warnings:")
    assert miss_idx < warn_idx


def test_t205_no_blockers_no_warnings_yields_ready_status_and_ok_reason():
    sc = ScrapedCourse(
        scrape_job_id="j", university_id=1,
        course_name="Bachelor of Computer Science",
        degree_level="Bachelor's", category="Computer Science & IT",
        study_mode="On Campus", course_location="Sydney",
        duration="3 years", intake_months=["February"],
        international_fee=45000, description="Great.",
        academic_level="Year 12", academic_score=85,
        ielts_overall=6.5, other_requirement="Personal statement",
    )
    comp = compute_completeness(sc)
    decision = decide_eligibility(sc, comp)
    assert decision.status == "ready"
    assert decision.reason == "ok"


# ─── T206: sibling-cache english-test backfill ──────────────────────────
def test_t206_backfill_fills_empty_slot_from_same_degree_bucket():
    """Two Master's siblings agree on IELTS=6.5 (Week 1 Prompt 6 requires
    consensus ≥ 2); a third Master's row with no English fields should
    be back-filled. Bachelor's siblings are in a different bucket and
    must not be polluted by the Master's value.

    Each seed evidence row uses ``method="regex"`` so the Week 1 Prompt
    4 source-type gate accepts it.
    """
    _seed_ev = [{
        "field_key": "ielts_overall",
        "value": 6.5,
        "method": "regex",
        "confidence": 0.9,
    }]
    results: list[dict] = [
        {
            "name": "Master of A",
            "url": "https://x/a",
            "payload": {"course_name": "Master of A", "degree_level": "Master",
                        "pte_overall": 58, "ielts_overall": 6.5},
            "evidence": list(_seed_ev),
        },
        {
            "name": "Master of D",
            "url": "https://x/d",
            "payload": {"course_name": "Master of D", "degree_level": "Master",
                        "ielts_overall": 6.5},
            "evidence": list(_seed_ev),
        },
        {
            "name": "Master of B",
            "url": "https://x/b",
            "payload": {"course_name": "Master of B", "degree_level": "Master"},
            "evidence": [],
        },
        {
            "name": "Bachelor of C",
            "url": "https://x/c",
            "payload": {"course_name": "Bachelor of C", "degree_level": "Bachelor"},
            "evidence": [],
        },
    ]
    n_filled = asyncio.run(backfill_english_from_siblings(results))
    assert n_filled >= 1
    # Master sibling B (results[2]) got back-filled for IELTS from
    # Masters A + D (results[0] + results[1]).
    # NOTE: PTE is intentionally excluded from _SIBLING_BACKFILL_SLOTS to
    # prevent cross-level contamination (only IELTS is backfilled globally).
    assert results[2]["payload"]["ielts_overall"] == 6.5
    assert "pte_overall" not in results[2]["payload"]
    # Bachelor row (results[3]) was NOT touched.
    assert "pte_overall" not in results[3]["payload"]
    assert "ielts_overall" not in results[3]["payload"]
    # Evidence rows annotated as sibling_cache:* so the review modal
    # can show provenance. Provenance fields must be present.
    sc_evidence = [e for e in results[2]["evidence"] if e.get("field_key") == "ielts_overall"]
    assert sc_evidence, "Backfilled course must have an ielts_overall evidence row"
    ev = sc_evidence[0]
    assert ev["method"].startswith("sibling_cache:"), f"Bad method: {ev['method']}"
    assert ev.get("source_method"), "source_method must be set in evidence row"
    assert ev.get("consensus_count", 0) >= 1, "consensus_count must be >= 1"
    assert ev.get("bucket"), "bucket must be set in evidence row"


def test_t206_backfill_no_op_when_no_sibling_has_data():
    """Empty buckets must produce zero fills (and not raise)."""
    results = [
        {
            "name": "Master of X",
            "url": "https://x/x",
            "payload": {"course_name": "Master of X", "degree_level": "Master"},
            "evidence": [],
        }
    ]
    n_filled = asyncio.run(backfill_english_from_siblings(results))
    assert n_filled == 0


# ─── T205 (regression): Missing must NOT duplicate blocker labels ───────
def test_t205_missing_section_excludes_fields_already_in_blockers():
    """If ``courseName`` is BOTH a hard blocker (empty name) AND in the
    completeness ``missing`` list, the Node format prints it once in
    Publish blocked and de-dups it from Missing. Pins that behaviour so
    a refactor of the Missing-filter doesn't reintroduce the duplicate.
    """
    sc = ScrapedCourse(
        scrape_job_id="j", university_id=1,
        course_name=None, degree_level=None,
    )
    comp = compute_completeness(sc)
    decision = decide_eligibility(sc, comp)
    blockers_section = decision.reason.split(" | ")[0]
    assert "courseName" in blockers_section
    if " | Missing: " in decision.reason:
        missing_section = decision.reason.split(" | Missing: ", 1)[1].split(" | ")[0]
        # courseName already named in blockers — must not repeat in Missing.
        miss_items = [t.strip() for t in missing_section.split(",")]
        assert "courseName" not in miss_items, (
            f"courseName leaked into Missing despite being a blocker: "
            f"{decision.reason}"
        )


def test_t205_no_missing_section_when_completeness_complete():
    """When the only failure is a single warning (no missing fields),
    the Missing section is omitted entirely — sections are
    drop-on-empty per Node spec.
    """
    sc = ScrapedCourse(
        scrape_job_id="j", university_id=1,
        course_name="Bachelor of Computer Science",
        degree_level="Bachelor's", category="Computer Science & IT",
        study_mode="On Campus", course_location="Sydney",
        duration="3 years", intake_months=["February"],
        international_fee=45000, description="Great.",
        academic_level="Year 12", academic_score=85,
        ielts_overall=6.5, other_requirement="Personal statement",
    )
    comp = compute_completeness(sc)
    decision = decide_eligibility(sc, comp)
    assert "Missing:" not in decision.reason


# ─── T209: TIMING + DONE log line shape ─────────────────────────────────
def test_t209_infer_log_level_rules():
    """Cheap unit check — confirms the orchestrator's level inference
    will flag DONE/TIMING messages with sensible buckets so T210 colour
    mapping picks them up. Ports the rule-table at the top of
    orchestrator.py.
    """
    from app.services.scraper.orchestrator import infer_log_level

    assert infer_log_level("[STAGE] saved: Bachelor of X") == "success"
    assert infer_log_level("[ERROR] something") == "error"
    assert infer_log_level("[STAGE] error on Y") == "error"
    assert infer_log_level("[FALLBACK] AI enriching ...") == "fallback"
    assert infer_log_level("[EXTRACT] 1/12: foo") == "extract"
    assert infer_log_level("plain status line") == "info"


def test_t209_orchestrator_run_scrape_emits_timing_and_done_payloads():
    """Source-level sentinel that survives refactors: assert run_scrape
    actually contains the TIMING + DONE emit calls with the exact
    payload keys the React log viewer parses
    (`event="done"` branch at scraping.tsx:1630 reads
    totalFound / imported / skipped / errors).

    Catches the regression class the architect flagged: if someone
    refactors run_scrape and removes/renames either the literal log
    string OR the typed payload keys, the UI would silently stop
    rendering the DONE row — with only an `infer_log_level` unit test,
    that regression would land undetected. This test fails loudly.
    """
    import inspect

    from app.services.scraper.orchestrator import run_scrape

    src = inspect.getsource(run_scrape)

    # Human-readable TIMING line the operator sees in the log pane.
    assert "[TIMING]" in src
    assert "Total:" in src and "Avg:" in src and "Concurrency:" in src

    # The DONE row the React `event === "done"` branch renders.
    assert "══ DONE ══" in src
    # Typed payload keys consumed by scraping.tsx's done renderer.
    for key in ("totalFound", "imported", "skipped", "errors"):
        assert key in src, (
            f"DONE payload key {key!r} missing from run_scrape — the "
            f"React log viewer's done-row branch expects it."
        )

    # Both rows must be tagged with explicit ``level=`` so T210 colour
    # mapping picks them up rather than falling through to phase
    # heuristics.
    assert 'level="info"' in src or "level='info'" in src
    assert 'level="success"' in src or "level='success'" in src
