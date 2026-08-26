"""Smoke tests for the ported scraper extractors. Each test feeds a small,
realistic HTML/text snippet through the extractor and asserts the
expected values come back. These tests run offline (no network)."""
from __future__ import annotations

import asyncio

from app.services.scraper.extractors import duration, english_test, fee, intake


def _run(coro):
    return asyncio.run(coro)


# --- Fee ---------------------------------------------------------------------
def test_fee_extracts_international_aud_per_year():
    html = """
    <html><body>
      <h2>International tuition fees</h2>
      <p>The international tuition fee for this program is A$42,000 per year (2026).</p>
      <p>Graduate salary outcomes: $85,000.</p>
    </body></html>
    """
    out = _run(fee.extract(html, "https://x", country="Australia"))
    assert len(out) == 1
    n = out[0].normalized
    assert n["international_fee"] == 42000
    assert n["currency"] == "AUD"
    assert n["fee_term"] == "Annual"
    assert n["fee_year"] == 2026


def test_fee_ignores_salary_only_pages():
    html = "<p>Average graduate salary: $95,000 per year.</p>"
    out = _run(fee.extract(html, "https://x"))
    assert out == []


def test_fee_no_emit_without_tuition_or_intl_context():
    # A page mentioning a $25,000 figure with no tuition/international cue
    # (e.g. a scholarship value, a deposit, a building cost) must NOT be
    # labelled as the international tuition fee.
    html = "<p>Annual scholarship value: $25,000 awarded to top students.</p>"
    out = _run(fee.extract(html, "https://x"))
    assert out == []


def test_fee_picks_intl_over_domestic_when_both_present():
    html = """
    <table>
      <tr><td>Domestic tuition</td><td>$8,500</td></tr>
      <tr><td>International tuition (per year)</td><td>$45,000</td></tr>
    </table>
    """
    out = _run(fee.extract(html, "https://x"))
    assert out and out[0].normalized["international_fee"] == 45000


# --- UWL regression: international fee from Angular SSR JSON blob -------------
# UWL course pages are served statically (Scrape.do render=False). The
# nationality-switcher <select> options are populated client-side, so on the
# static HTML it carries only a partial/UK option set and used to yield a FALSE
# "domestic only" verdict — dropping the international fee even though the SSR
# JSON blob (field_p_cv_int_main_fee) clearly contains it. Per operator policy:
# if a course is offered to international students it always has an intl fee.
def test_fee_uwl_json_blob_international_fee():
    # JSON blob present with both intl + UK fees; an empty (JS-populated) select
    # is also present. The blob must win and produce the £16,750 intl fee.
    html = (
        '<select id="nationality_pricing_input_mobile"></select>'
        '<script>window.data={'
        '"field_p_cv_int_main_fee":{"target_id":1994,"name":"16750"},'
        '"field_p_cv_uk_eu_main_fee":{"target_id":2012,"name":"9790"}'
        '};</script>'
    )
    out = _run(fee.extract(html, "https://www.uwl.ac.uk/course/undergraduate/forensic-science"))
    assert out and out[0].normalized["international_fee"] == 16750
    assert out[0].normalized["currency"] == "GBP"


def test_fee_uwl_json_blob_domestic_only_when_no_intl_fee():
    # UK fee only, no int_main_fee → domestic-only course → no fee emitted.
    html = (
        '<select id="nationality_pricing_input_mobile"></select>'
        '<script>window.data={'
        '"field_p_cv_uk_eu_main_fee":{"target_id":2012,"name":"9790"}'
        '};</script>'
    )
    out = _run(fee.extract(html, "https://www.uwl.ac.uk/course/undergraduate/learning-disabilities-nursing-foundation"))
    assert out == []


def test_fee_uwl_json_blob_picks_first_intl_fee():
    # Two int_main_fee entries (main course + a linked/related course). The
    # FIRST is the headline full-time fee shown in the fees panel.
    html = (
        '<script>window.data={'
        '"field_p_cv_int_main_fee":{"target_id":1994,"name":"16750"},'
        '"field_p_cv_uk_eu_main_fee":{"target_id":2012,"name":"9790"},'
        '"field_p_cv_int_main_fee":{"target_id":1996,"name":"11160"}'
        '};</script>'
    )
    out = _run(fee.extract(html, "https://www.uwl.ac.uk/course/undergraduate/forensic-science"))
    assert out and out[0].normalized["international_fee"] == 16750


# --- IELTS / PTE / TOEFL / Cambridge / Duolingo -----------------------------
def test_english_ielts_overall_with_no_band_below():
    html = "<p>IELTS Academic overall 6.5 with no individual band below 6.0.</p>"
    out = {r.field_key: r for r in _run(english_test.extract(html, "https://x"))}
    assert "ielts_overall" in out
    n = out["ielts_overall"].normalized
    assert n["ielts_overall"] == 6.5 and n["ielts_listening"] == 6.0


# --- UOW regression: explicit course-level IELTS skill table -----------------
def test_english_uow_skill_table_maps_bands_by_column_header():
    html = """
    <section>
      <h3>English Language Requirements</h3>
      <table>
        <thead>
          <tr>
            <th>English Test</th>
            <th>Overall Score</th>
            <th>Reading</th>
            <th>Writing</th>
            <th>Listening</th>
            <th>Speaking</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>IELTS Academic</td>
            <td>7.5</td>
            <td>7.0</td>
            <td>7.0</td>
            <td>8.0</td>
            <td>8.0</td>
          </tr>
        </tbody>
      </table>
    </section>
    """
    out = {
        r.field_key: r
        for r in _run(
            english_test.extract(
                html,
                "https://www.uow.edu.au/study/courses/master-of-teaching-secondary/",
            )
        )
    }
    result = out["ielts_overall"]
    assert result.method == "ielts_skill_table"
    assert result.confidence == 0.98
    assert result.normalized == {
        "ielts_overall": 7.5,
        "ielts_reading": 7.0,
        "ielts_writing": 7.0,
        "ielts_listening": 8.0,
        "ielts_speaking": 8.0,
    }


# --- UWL regression: split overall (banner) + per-band floor (prose) ---------
# https://www.uwl.ac.uk/course/undergraduate/forensic-science states the overall
# in a banner ("6.0 IELTS or above") and the per-band floor in body prose
# ("a minimum of IELTS 5.5 for each of the four individual components"). Before
# the fix the broad fallback grabbed the per-band 5.5 as the overall, so the UI
# showed IELTS 5.5 instead of the true overall 6.0 / each band 5.5.
def test_english_ielts_uwl_split_banner_and_prose():
    html = (
        "<p>6.0 IELTS or above. You need to meet our English language requirement "
        "- a minimum of IELTS 5.5 for each of the four individual components "
        "(Reading, Writing, Speaking and Listening).</p>"
    )
    out = {r.field_key: r for r in _run(english_test.extract(html, "https://www.uwl.ac.uk"))}
    assert "ielts_overall" in out
    n = out["ielts_overall"].normalized
    assert n["ielts_overall"] == 6.0
    assert n["ielts_listening"] == 5.5 and n["ielts_reading"] == 5.5
    assert n["ielts_writing"] == 5.5 and n["ielts_speaking"] == 5.5


def test_english_ielts_uwl_split_ielts_first_order():
    # Same split structure but the overall states the keyword first:
    # "IELTS 6.5 or above ... minimum of IELTS 6.0 in each component".
    html = (
        "<p>IELTS 6.5 or above with a minimum of IELTS 6.0 in each component.</p>"
    )
    out = {r.field_key: r for r in _run(english_test.extract(html, "https://x"))}
    assert "ielts_overall" in out
    n = out["ielts_overall"].normalized
    assert n["ielts_overall"] == 6.5 and n["ielts_listening"] == 6.0


def test_english_ielts_single_signal_does_not_trigger_split_pattern():
    # Pattern 4.6 requires BOTH an overall "or above" clause AND a per-band
    # "each component" clause. When only the per-band clause is present (no
    # overall banner), Pattern 4.6 must NOT fire — it must fall through to the
    # broad fallback rather than fabricate a higher overall. Asserted at the
    # _ielts() unit level to isolate pattern precedence from extract()'s
    # evidence-guarding of bare overalls.
    from app.services.scraper.extractors.english_test import _ielts

    res = _ielts("a minimum of IELTS 5.5 for each of the four individual components")
    assert res is not None
    # Broad fallback result (single bare score → overall, no per-band floor),
    # NOT the split-pattern result that would set all four bands to 5.5.
    assert res["overall"] == 5.5
    assert res["listening"] is None


# --- UWL PhD regression: "no element under" IELTS band phrasing --------------
# UWL research-degree pages (/course/research/…) use a different phrase:
# "IELTS score of 6.5 (with no element under 6.0)" instead of the taught-
# course phrase "no band below".  Pattern 1b must capture the band floor
# because "element" was not in the alternation before this fix.
def test_english_ielts_uwl_phd_no_element_under():
    from app.services.scraper.extractors.english_test import _ielts

    text = (
        "IELTS 6.5 (with no element under 6.0). "
        "We look for individuals with a strong academic background."
    )
    res = _ielts(text)
    assert res is not None, "Pattern 1b must match 'no element under'"
    assert res["overall"] == 6.5
    assert res["listening"] == 6.0
    assert res["reading"] == 6.0


def test_english_ielts_uwl_phd_no_element_under_extract():
    # End-to-end via extract() to confirm the ExtractionResult includes bands.
    html = (
        "<p>6.5 IELTS or above</p>"
        "<p>An IELTS (International English Language Testing System) score of "
        "6.5 (with no element under 6.0). We look for individuals with:</p>"
    )
    out = {r.field_key: r for r in _run(english_test.extract(html, "https://www.uwl.ac.uk"))}
    assert "ielts_overall" in out
    n = out["ielts_overall"].normalized
    assert n["ielts_overall"] == 6.5
    assert n["ielts_listening"] == 6.0 and n["ielts_reading"] == 6.0


# --- UWL research-degree fee: no-blob + no-select safety net -----------------
# When a UWL /course/research/ page has neither the Angular SSR JSON blob nor
# a nationality-pricing select (e.g. a newly-added programme not yet wired to
# the widget), the generic fee scanner must NOT extract the domestic PhD rate
# (e.g. £6,000) as the international fee.  The safety net returns domestic-only.
def test_fee_uwl_research_max_blob_returns_fulltime_fee():
    # UWL /course/research/ pages embed 4 fee options in the SSR blob:
    #   14000 (generic CMS placeholder), 16000 (full-time international ← correct),
    #   7000 and 8000 (part-time per-year rates).
    # The research URL guard takes the MAX of all int blob values so the
    # full-time fee (16000) wins over the generic placeholder (14000).
    # This is consistent with the JS dropdown default shown to users.
    html = (
        "<html><body>"
        # Two blob sections (UWL embeds SSR JSON twice)
        '<script>"field_p_cv_int_main_fee":{"target_id":1,"name":"14000"},'
        '"field_p_cv_uk_eu_main_fee":{"target_id":2,"name":"4400"}</script>'
        '<script>"field_p_cv_int_main_fee":{"target_id":228,"name":"16000"},'
        '"field_p_cv_uk_eu_main_fee":{"target_id":1586,"name":"4900"},'
        '"field_p_cv_int_main_fee":{"target_id":3,"name":"7000"},'
        '"field_p_cv_int_main_fee":{"target_id":4,"name":"8000"}</script>'
        "</body></html>"
    )
    results = _run(fee.extract(html, "https://www.uwl.ac.uk/course/research/mathematics", country="United Kingdom"))
    intl_fees = [r for r in results if r.normalized and r.normalized.get("international_fee")]
    assert intl_fees, "Expected international_fee for UWL research page with multi-option blob"
    assert intl_fees[0].normalized["international_fee"] == 16000, (
        f"Expected 16000 (full-time max), got {intl_fees[0].normalized['international_fee']}"
    )


def test_english_pte_score():
    html = "<p>PTE Academic 64 overall.</p>"
    out = {r.field_key: r for r in _run(english_test.extract(html, "https://x"))}
    assert out["pte_overall"].normalized["pte_overall"] == 64.0


def test_english_toefl_score():
    html = "<p>TOEFL iBT: 90 with no section below 20.</p>"
    out = {r.field_key: r for r in _run(english_test.extract(html, "https://x"))}
    assert out["toefl_overall"].normalized["toefl_overall"] == 90.0


def test_english_duolingo_and_cambridge():
    html = "<p>Cambridge C1 Advanced: 185. Duolingo English Test: 110.</p>"
    out = {r.field_key: r for r in _run(english_test.extract(html, "https://x"))}
    assert out["cambridge_overall"].value == 185.0
    assert out["duolingo_overall"].value == 110.0


# --- VIT regression: PR-1.5 hot-fix #2 ---------------------------------------
# Real prose copied from https://vit.edu.au/mba/mba-project-management. Before
# the fix, all 5 IELTS patterns (and their PTE/TOEFL twins) blocked on the word
# "score" sitting between "Overall" and the digit, so 100% of VIT staged rows
# landed with IELTS=— even though the page plainly stated 6.5.
def test_english_ielts_overall_score_x_with_no_band_below_y():
    html = (
        "<p>English test results IELTS Academic: Overall score 6.5, "
        "with no band below 6.0, or Equivalent results in another approved test.</p>"
    )
    out = {r.field_key: r for r in _run(english_test.extract(html, "https://vit.edu.au"))}
    assert "ielts_overall" in out
    n = out["ielts_overall"].normalized
    assert n["ielts_overall"] == 6.5 and n["ielts_listening"] == 6.0


def test_english_pte_overall_score_x_with_no_skill_below_y():
    html = (
        "<p>PTE Academic: Overall score 58, with no communicative skill below 50.</p>"
    )
    out = {r.field_key: r for r in _run(english_test.extract(html, "https://vit.edu.au"))}
    assert out["pte_overall"].normalized["pte_overall"] == 58.0
    assert out["pte_overall"].normalized["pte_listening"] == 50.0


def test_english_toefl_overall_score_x_with_no_section_below_y():
    html = (
        "<p>TOEFL iBT: Overall score 87, with no section below 17.</p>"
    )
    out = {r.field_key: r for r in _run(english_test.extract(html, "https://vit.edu.au"))}
    assert out["toefl_overall"].normalized["toefl_overall"] == 87.0
    assert out["toefl_overall"].normalized["toefl_listening"] == 17.0


# --- Equivalence-table fallback (PR-1.5 hot-fix #3) --------------------------
# Real VIT layout, distilled. Page prose only states IELTS=6.5; PTE/TOEFL/CAE
# live exclusively in this multi-row equivalence table. Before the parser was
# added, has_pte/toefl/cae rates dropped from 99.6% to ~45% in prod because
# vision OCR couldn't reliably pick the right cell from the table image.
_VIT_EQUIV_TABLE_HTML = """
<p>English test results IELTS Academic: Overall score 6.5, with no band below 6.0.</p>
<table>
  <thead>
    <tr>
      <th colspan="2"><strong>IELTS (Academic)</strong></th>
      <th colspan="5"><strong>PTE (Academic)</strong></th>
      <th colspan="5"><strong>TOEFL IBT Overall (as per IELTS website)</strong></th>
      <th colspan="2"><strong>(CAE) Cambridge English scale score</strong></th>
      <th colspan="2"><strong>(KITE) Kaplan</strong></th>
    </tr>
    <tr>
      <th>overall</th><th>No band less than</th>
      <th>overall</th><th>Listening</th><th>Reading</th><th>Speaking</th><th>Writing</th>
      <th>overall</th><th>Listening</th><th>Reading</th><th>Speaking</th><th>Writing</th>
      <th>overall</th><th> </th>
      <th>overall</th><th> </th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td rowspan="2"><strong>5.5</strong></td><td rowspan="2"><strong>5</strong></td>
      <td rowspan="2"><strong>39</strong></td><td colspan="4">no band &lt; 5.0</td>
      <td rowspan="2"><strong>51</strong></td><td colspan="4">no band &lt; 5.0</td>
      <td rowspan="2"><strong>162</strong></td><td>no band &lt; 5.0</td>
      <td rowspan="2"><strong>410</strong></td><td>no band &lt; 5.0</td>
    </tr>
    <tr><td>33</td><td>36</td><td>24</td><td>29</td><td>8</td><td>8</td><td>14</td><td>9</td><td>160</td><td>373</td></tr>
    <tr>
      <td rowspan="2"><strong>6.5</strong></td><td rowspan="2"><strong>6</strong></td>
      <td rowspan="2"><strong>55</strong></td><td colspan="4">no band &lt; 6.0</td>
      <td rowspan="2"><strong>81</strong></td><td colspan="4">no band &lt; 6.0</td>
      <td rowspan="2"><strong>176</strong></td><td>no band &lt; 6.0</td>
      <td rowspan="2"><strong>478</strong></td><td>no band &lt; 6.0</td>
    </tr>
    <tr><td>47</td><td>48</td><td>54</td><td>51</td><td>16</td><td>16</td><td>19</td><td>19</td><td>169</td><td>444</td></tr>
  </tbody>
</table>
"""


def test_english_equivalence_table_fills_pte_toefl_cae_when_only_ielts_in_prose():
    out = {
        r.field_key: r
        for r in _run(english_test.extract(_VIT_EQUIV_TABLE_HTML, "https://vit.edu.au"))
    }
    assert out["ielts_overall"].value == 6.5
    assert out["ielts_overall"].method == "regex"
    # PTE/TOEFL/CAE come from the IELTS=6.5 row of the equivalence table.
    assert out["pte_overall"].value == 55.0
    assert out["pte_overall"].method == "equivalence_table"
    assert out["toefl_overall"].value == 81.0
    assert out["toefl_overall"].method == "equivalence_table"
    assert out["cambridge_overall"].value == 176.0
    assert out["cambridge_overall"].method == "equivalence_table"


def test_english_equivalence_table_does_not_overwrite_prose_extraction():
    """When prose already gave us PTE, the table fallback must not clobber it."""
    html = (
        "<p>IELTS Academic: Overall score 6.5, with no band below 6.0. "
        "PTE Academic: Overall score 58, with no communicative skill below 50.</p>"
        + _VIT_EQUIV_TABLE_HTML.split("</p>", 1)[1]
    )
    results = _run(english_test.extract(html, "https://vit.edu.au"))
    pte_results = [r for r in results if r.field_key == "pte_overall"]
    # Should have only one PTE result and it must come from prose, not table.
    assert len(pte_results) == 1
    assert pte_results[0].value == 58.0
    assert pte_results[0].method == "regex"


def test_english_equivalence_table_skipped_when_no_ielts_extracted():
    """No prose IELTS → no anchor for the table lookup → no fallback fires."""
    html = _VIT_EQUIV_TABLE_HTML.replace(
        "English test results IELTS Academic: Overall score 6.5, with no band below 6.0.",
        "English requirements: contact admissions for details.",
    )
    out = {r.field_key: r for r in _run(english_test.extract(html, "https://vit.edu.au"))}
    assert "ielts_overall" not in out
    assert "pte_overall" not in out
    assert "toefl_overall" not in out


def test_english_equivalence_table_ignores_non_equivalence_tables():
    """A page with a fees table and IELTS prose must not match the fees table."""
    html = """
    <p>IELTS Academic: Overall score 6.5, with no band below 6.0.</p>
    <table>
      <thead><tr><th>Year</th><th>Tuition</th></tr></thead>
      <tbody><tr><td>2026</td><td>$28000</td></tr></tbody>
    </table>
    """
    out = {r.field_key: r for r in _run(english_test.extract(html, "https://x"))}
    assert out["ielts_overall"].value == 6.5
    # No PTE/TOEFL/CAE because this table isn't an equivalence table.
    assert "pte_overall" not in out
    assert "toefl_overall" not in out
    assert "cambridge_overall" not in out


def test_english_equivalence_fallback_propagates_pte_listening_and_writing():
    """Task 62: when a multi-column Pearson header exposes Listening/Writing
    sub-columns, _equivalence_fallback must include pte_listening and
    pte_writing in the normalized dict of the pte_overall ExtractionResult.

    Before this fix, sub-skill keys were silently dropped — the loop
    generated ``pte_listening_overall`` (wrong) instead of treating them as
    already-namespaced field keys.  The pipeline writes normalized[k] to the
    payload, so this is the gate between extraction and DB persistence."""
    html = """
    <p>IELTS Academic: Overall score 6.5, with no band below 6.0.</p>
    <table>
      <thead>
        <tr>
          <th>IELTS</th>
          <th colspan="3">Pearson Test of English</th>
          <th>TOEFL iBT</th>
        </tr>
        <tr>
          <th>Overall</th>
          <th>Overall</th>
          <th>Listening</th>
          <th>Writing</th>
          <th>Overall</th>
        </tr>
      </thead>
      <tbody>
        <tr><td>6.5</td><td>58</td><td>50</td><td>50</td><td>79</td></tr>
        <tr><td>6.0</td><td>50</td><td>42</td><td>42</td><td>60</td></tr>
      </tbody>
    </table>
    """
    out = {
        r.field_key: r
        for r in _run(english_test.extract(html, "https://example.edu.au"))
    }
    pte = out.get("pte_overall")
    assert pte is not None, "pte_overall must be extracted from the equivalence table"
    assert pte.value == 58.0
    assert pte.method == "equivalence_table"
    assert pte.normalized.get("pte_listening") == 50.0, (
        "pte_listening must appear in normalized when the table has a Listening column"
    )
    assert pte.normalized.get("pte_writing") == 50.0, (
        "pte_writing must appear in normalized when the table has a Writing column"
    )


# --- Intake ------------------------------------------------------------------
def test_intake_parses_keyword_window():
    html = "<p>Available intakes: February, July and September.</p>"
    out = _run(intake.extract(html, "https://x"))
    months = out[0].normalized["intake_months"]
    assert "February" in months and "July" in months and "September" in months


def test_intake_parses_full_dates():
    html = "<p>Course start dates: 24 February 2026 and 15 July 2026.</p>"
    out = _run(intake.extract(html, "https://x"))
    n = out[0].normalized
    assert "February" in n["intake_months"] and "July" in n["intake_months"]
    assert n["intake_days"] in {15, 24}


# --- Duration ----------------------------------------------------------------
def test_duration_picks_standard_over_accelerated():
    html = """
    <p>Course duration: 3 years full-time.</p>
    <p>Accelerated stream: 1 year intensive study available.</p>
    """
    out = _run(duration.extract(html, "https://x"))
    n = out[0].normalized
    assert n["duration"] == 3.0 and n["duration_term"] == "Year"


def test_duration_handles_months():
    html = "<p>Program length: 18 months full-time.</p>"
    out = _run(duration.extract(html, "https://x"))
    n = out[0].normalized
    assert n["duration"] == 18.0 and n["duration_term"] == "Month"


# PR-1.5 prod regression: VIT MBA staged duration=10 Year because the loose
# `<num> <unit>` fallback (pattern 3) matched marketing copy like
# "over 10 years of industry partnerships". Tests below lock the contract:
# pattern 3 only fires when a duration-context word is in the same sentence
# AND no anti-context (experience/established/celebrating/...) is present.
def test_duration_rejects_years_experience_marketing_copy():
    """`10 years experience` is staff tenure, not program length."""
    html = "<p>Our staff have over 10 years experience in industry.</p>"
    out = _run(duration.extract(html, "https://x"))
    assert out == [], f"PR-1.5 regression: should not match staff tenure, got {out!r}"


def test_duration_rejects_anniversary_marketing_copy():
    html = "<p>Celebrating 10 years of academic excellence.</p>"
    out = _run(duration.extract(html, "https://x"))
    assert out == [], f"PR-1.5 regression: anniversary copy should not match, got {out!r}"


def test_duration_rejects_established_year_marketing_copy():
    html = "<p>Established in 2014, with 10 years of industry partnerships.</p>"
    out = _run(duration.extract(html, "https://x"))
    assert out == [], f"PR-1.5 regression: institutional history should not match, got {out!r}"


def test_duration_loose_fallback_still_matches_when_context_is_present():
    """Pattern-3 fallback still wins when duration context IS in the
    sentence — full-time without an explicit 'Course duration:' label."""
    html = "<p>Full-time study takes 4 years to complete.</p>"
    out = _run(duration.extract(html, "https://x"))
    n = out[0].normalized
    assert n["duration"] == 4.0 and n["duration_term"] == "Year"


def test_duration_real_signal_wins_over_marketing_noise():
    """Multi-sentence: the legitimate duration sentence must beat the
    rejected marketing-copy sentence — proves the filter rejects the
    bad signal entirely, not just demotes it."""
    html = """
    <p>Established 10 years ago by a team with 20 years experience.</p>
    <p>Course duration is 2 years full-time.</p>
    """
    out = _run(duration.extract(html, "https://x"))
    assert len(out) >= 1, "real duration signal should still extract"
    n = out[0].normalized
    assert n["duration"] == 2.0 and n["duration_term"] == "Year", (
        f"real 2-year duration should win, got {n!r}"
    )
