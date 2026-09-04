"""DOM-aware label-detection regression tests for the international-fee
extractor.

The bug class: tag-stripping flattens a label/value layout into a
single token run; an adjacent paragraph's currency figure (a
scholarship value, a deposit, a building cost) can sit close enough
to "International tuition" / "fees" that the proximity-scoring keyword
fallback picks the wrong number. The structural pre-pass reads the
value cell directly out of the DOM so the boundary collision can't
mislead it.

Only "international"-flavoured labels trigger the structural path —
domestic/ambiguous labels still go through the existing keyword
scoring so we don't accidentally promote a domestic fee to the
international tuition.
"""
from __future__ import annotations

import asyncio

from app.services.scraper.config.context import current_uni_config
from app.services.scraper.config.schema import UniConfig
from app.services.scraper.extractors import fee


def _run(coro):
    return asyncio.run(coro)


def test_explicit_international_fee_meta_beats_domestic_csp_body_amounts():
    html = """
    <head>
      <meta content="AU$47,040 (2027 annual)" name="fees_international">
      <meta content="AU$38,400 (2027 annual)" name="fees_domestic">
    </head>
    <body>
      <p>2027 Commonwealth supported places range from AU$4,908 to AU$18,025.</p>
    </body>
    """
    out = _run(
        fee.extract(
            html,
            "https://www.rmit.edu.au/study-with-us/example-mc292",
            country="Australia",
        )
    )
    assert len(out) == 1
    assert out[0].normalized == {
        "international_fee": 47040,
        "currency": "AUD",
        "fee_term": "Annual",
        "fee_year": 2027,
    }
    assert out[0].method == "fee.explicit_international_meta"
    assert out[0].confidence == 0.99


def test_domestic_or_ambiguous_fee_meta_does_not_trigger_international_prepass():
    html = """
    <head>
      <meta name="fees_domestic" content="AU$8,000 (2027 annual)">
      <meta name="course_fee" content="AU$9,000 (2027 annual)">
    </head>
    <body><p>Domestic tuition fees are AU$8,000 annually.</p></body>
    """
    out = _run(fee.extract(html, "https://example.edu/course"))
    assert not any(
        result.method == "fee.explicit_international_meta" for result in out
    )


def test_leeds_beckett_reads_international_tab_not_active_uk_fee():
    html = """
    <section id="fees-and-funding-component">
      <button role="tab" class="is-active">UK</button>
      <button role="tab">International</button>
      <div class="tabs__panel is-active">
        <div class="key-info__item">
          <div class="key-info__item-value">£9,790</div>
          <p class="key-info__item-title">UK</p>
        </div>
      </div>
      <div class="tabs__panel">
        <div class="key-info__item color-bg-green-int">
          <div class="key-info__item-value">£16,840</div>
          <p class="key-info__item-title">International 2026</p>
        </div>
      </div>
    </section>
    """
    out = _run(
        fee.extract(
            html,
            "https://www.leedsbeckett.ac.uk/courses/geography-bsc/",
            country="United Kingdom",
        )
    )
    assert len(out) == 1
    assert out[0].normalized == {
        "international_fee": 16840.0,
        "currency": "GBP",
        "fee_term": "Annual",
        "fee_year": 2026,
    }
    assert out[0].method == "fee.leeds_beckett_international_panel"


def test_leeds_beckett_blank_international_tab_never_falls_back_to_uk_fee():
    html = """
    <section id="fees-and-funding-component">
      <button role="tab" class="is-active">UK</button>
      <button role="tab">International</button>
      <div class="tabs__panel is-active">
        <div class="key-info__item">
          <div class="key-info__item-value">£9,790</div>
          <p class="key-info__item-title">UK</p>
        </div>
      </div>
      <div class="tabs__panel"></div>
    </section>
    """
    out = _run(
        fee.extract(
            html,
            "https://www.leedsbeckett.ac.uk/courses/unpublished-course/",
            country="United Kingdom",
        )
    )
    assert out == []


def test_leeds_beckett_preserves_full_course_international_fee_term():
    html = """
    <section id="fees-and-funding-component">
      <button role="tab" class="is-active">UK</button>
      <button role="tab">International</button>
      <div class="key-info__item color-bg-green-int">
        <div class="key-info__item-value">£3,350</div>
        <p class="key-info__item-title">
          International 2026. Full Course Tuition Fees.
        </p>
      </div>
    </section>
    """
    out = _run(
        fee.extract(
            html,
            "https://www.leedsbeckett.ac.uk/courses/example-pg-cert/",
            country="United Kingdom",
        )
    )
    assert len(out) == 1
    assert out[0].normalized["international_fee"] == 3350.0
    assert out[0].normalized["fee_term"] == "Full Course"


def test_strong_intl_fee_sibling_div_classifies_via_structural_pass():
    """ASA-style adjacent-div idiom: `<div><strong>International tuition
    fees</strong></div><div>A$42,000 per year</div>`. The keyword
    fallback's proximity scoring could otherwise lock onto an unrelated
    currency figure (a scholarship value) elsewhere on the page."""
    html = (
        '<div><strong>Scholarships</strong></div>'
        '<div>Apply for a $30,000 merit award.</div>'
        '<div><strong>International tuition fees</strong></div>'
        '<div>A$42,000 per year (2026)</div>'
    )
    out = _run(fee.extract(html, "https://e/x", country="Australia"))
    assert out, "structural pre-pass should fire on <strong>International tuition fees</strong>"
    n = out[0].normalized
    assert n["international_fee"] == 42000, (
        f"Expected $42,000 from the labelled cell, got {n!r}. "
        f"Pre-fix the keyword fallback could lock onto the $30,000 "
        f"scholarship figure via proximity scoring."
    )
    assert n["currency"] == "AUD"
    assert n["fee_term"] == "Annual"
    assert n["fee_year"] == 2026
    assert out[0].method.startswith("fee.structural")


def test_dt_dd_intl_fee_classifies_via_structural_pass():
    """`<dt>International tuition fees</dt><dd>$45,000 per year</dd>`
    — definition-list shape with explicit international label."""
    html = (
        "<dl><dt>International tuition fees</dt><dd>$45,000 per year</dd></dl>"
        "<p>The deposit required to confirm enrolment is $5,500.</p>"
    )
    out = _run(fee.extract(html, "https://e/x"))
    assert out
    n = out[0].normalized
    assert n["international_fee"] == 45000, (
        f"<dt>/<dd> structural pre-pass must read only the dd value. "
        f"Got {n!r}."
    )
    assert n["fee_term"] == "Annual"
    assert out[0].method.startswith("fee.structural")


def test_th_td_intl_fee_classifies_via_structural_pass():
    """`<th>International fees</th><td>A$38,500</td>` — table key/value
    shape. A neighbouring row with a domestic figure must not bleed
    into the international fee capture."""
    html = (
        "<table>"
        "<tr><th>Domestic fees</th><td>$8,500</td></tr>"
        "<tr><th>International fees</th><td>A$38,500</td></tr>"
        "</table>"
    )
    out = _run(fee.extract(html, "https://e/x"))
    assert out
    n = out[0].normalized
    assert n["international_fee"] == 38500, (
        f"<th>/<td> structural pre-pass must pick the international row. "
        f"Got {n!r}."
    )
    assert out[0].method.startswith("fee.structural")


def test_fee_structural_skips_ambiguous_tuition_label_for_domestic():
    """Bare `<strong>Tuition fees</strong>` is ambiguous (could be
    domestic OR international) and is therefore NOT in the structural
    label whitelist. The existing keyword fallback (with intl-context
    scoring) handles this case so we don't accidentally claim a
    domestic-only fee as the international tuition."""
    html = (
        '<div><strong>Tuition fees</strong></div>'
        '<div>$8,000 per year for domestic students.</div>'
    )
    out = _run(fee.extract(html, "https://e/x"))
    # No international cue anywhere on the page; the keyword fallback
    # rejects (no _INTL_CTX hit). Either no result or the structural
    # path didn't claim it.
    structural = [r for r in out if r.method.startswith("fee.structural")]
    assert not structural, (
        f"Bare 'Tuition fees' must NOT trigger the structural pre-pass — "
        f"the label is ambiguous. Got {structural!r}."
    )


def test_fee_structural_does_not_misfire_on_random_strong_tags():
    """`<strong>Apply Now</strong>` / `<strong>Contact</strong>` are
    not fee labels; only the explicit international-fee whitelist
    triggers the structural walk."""
    html = (
        '<a><strong>Apply Now</strong></a>'
        '<div><strong>Contact</strong></div><div>info@uni.edu</div>'
        '<p>The international tuition fee for this program is '
        'A$42,000 per year (2026).</p>'
    )
    out = _run(fee.extract(html, "https://e/x", country="Australia"))
    assert out and out[0].normalized["international_fee"] == 42000
    # Came from the keyword fallback, not the structural pre-pass.
    assert not out[0].method.startswith("fee.structural")


def test_swinburne_international_panel_beats_domestic_yearly_and_total_fees():
    html = """
    <div class="course-fees__container course-fees domestic">
      <h2>2026 tuition fees</h2>
      <div class="course-fees__block domestic">
        <h4 class="course-fees__sub-title">Yearly fee* ($AUD)</h4>
        <p class="course-fees__total">$17,399.00</p>
      </div>
      <div class="course-fees__block domestic">
        <h4 class="course-fees__sub-title">Total fee* ($AUD)</h4>
        <p class="course-fees__total">$52,197.00</p>
      </div>
    </div>
    <div class="course-fees__container course-fees international">
      <h2>2026 tuition fees</h2>
      <div class="course-fees__block international">
        <h4 class="course-fees__sub-title">Yearly fee* ($AUD)</h4>
        <p class="course-fees__total">$44,840.00</p>
      </div>
      <div class="course-fees__block international">
        <h4 class="course-fees__sub-title">SSAF fee* ($AUD)</h4>
        <p class="course-fees__total">$373.00</p>
      </div>
    </div>
    """
    out = _run(
        fee.extract(
            html,
            "https://www.swinburne.edu.au/course/undergraduate/bachelor-of-screen-production/",
            country="Australia",
        )
    )
    assert out
    assert out[0].normalized == {
        "international_fee": 44840,
        "currency": "AUD",
        "fee_term": "Annual",
        "fee_year": 2026,
    }
    assert out[0].method == "fee.swinburne_international_panel"


def test_swinburne_zero_international_placeholder_suppresses_domestic_fee():
    html = """
    <div class="course-fees__container course-fees domestic">
      <div class="course-fees__block domestic">
        <h4 class="course-fees__sub-title">Total fee* ($AUD)</h4>
        <p class="course-fees__total">$73,080.00</p>
      </div>
    </div>
    <div class="course-fees__container course-fees international">
      <h2>2026 fees</h2>
      <div class="course-fees__block international">
        <h4 class="course-fees__sub-title">Yearly fee* ($AUD)</h4>
        <p class="course-fees__total">$0.00</p>
      </div>
    </div>
    """
    out = _run(
        fee.extract(
            html,
            "https://www.swinburne.edu.au/course/postgraduate/master-of-project-management/",
            country="Australia",
        )
    )
    assert out == []


def test_uts_domestic_total_is_never_international_fee():
    """UTS defaults to a Domestic audience card in the browser."""
    html = """
    <section>
      <div>COURSE FEES</div>
      <div>Indicative total tuition fee for domestic students</div>
      <div>$74,949.60</div>
      <a>More on fees</a>
    </section>
    """
    out = _run(
        fee.extract(
            html,
            "https://www.uts.edu.au/courses/master-of-philosophy-in-science",
            country="Australia",
        )
    )
    assert out == []


def test_uts_international_large_total_is_accepted_as_full_course():
    """Explicit full-course tuition may legitimately exceed the generic cap."""
    html = """
    <section>
      <div>COURSE FEES</div>
      <div>Indicative total tuition fee for international students</div>
      <div>$251,437.94</div>
      <a>More on fees</a>
    </section>
    """
    out = _run(
        fee.extract(
            html,
            "https://www.uts.edu.au/courses/bachelor-of-engineering-honours-biomedical",
            country="Australia",
        )
    )
    assert out
    assert out[0].normalized["international_fee"] == 251_437.94
    assert out[0].normalized["fee_term"] == "Full Course"
    assert out[0].method == "fee.uts_international_total"


def test_uts_total_is_annualised_by_duration_not_session_wording():
    from app.services.scraper.config.loader import get_config_for_host
    from app.services.scraper.per_course_browser import _extended_extract
    from app.services.scraper.recipe_rules import apply_recipe_rules

    html = """
    <section>
      <h3>COURSE FEES</h3>
      <p>Indicative total tuition fee for international students<br>
         $89,556.00</p>
      <div>
        <strong>Indicative first-year tuition fee:</strong> $43,900.00
        You can choose to pay your fees upfront per session.
      </div>
    </section>
    """
    out = _run(
        fee.extract(
            html,
            "https://www.uts.edu.au/courses/master-of-indigenous-health-research",
            country="Australia",
        )
    )
    assert out
    result = out[0]
    assert result.normalized["international_fee"] == 89_556.0
    assert result.normalized["fee_term"] == "Full Course"
    assert result.method == "fee.uts_international_total"

    browser_values, browser_evidence = _run(
        _extended_extract(
            html,
            "https://www.uts.edu.au/courses/master-of-indigenous-health-research",
            {
                "international_fee": 43_900,
                "fee_term": "Session",
            },
            override=True,
        )
    )
    assert browser_values["international_fee"] == 89_556.0
    assert browser_values["fee_term"] == "Full Course"
    assert browser_values["currency"] == "AUD"
    assert any(
        row["field_key"] == "fee_term"
        and row["method"] == "per_course_browser_extended"
        for row in browser_evidence
    )

    config = get_config_for_host(
        hostname="www.uts.edu.au",
        name="University of Technology Sydney",
        scrape_url="https://www.uts.edu.au/courses",
    )
    assert config.extraction.force_browser is True
    assert config.extraction.skip_remote_ai_enrichment is True
    assert config.extraction.max_parallel_fetch == 4
    assert config.extraction.english.trust_vision_ocr is False
    assert config.extraction.english.skip_vision_when_core_found is True
    assert config.extraction.recovery_sweep_max_items == 0
    assert config.extraction.recovery_sweep_time_budget_seconds == 0
    assert config.discovery.allow_url_patterns
    payload = {
        **browser_values,
        "duration": 2,
        "duration_term": "Year",
    }
    final = apply_recipe_rules(payload, config.extraction.fees.model_dump())
    assert final["international_fee"] == 44_778
    assert final["fee_term"] == "Annual"


def test_nearest_audience_label_wins_when_both_fee_rows_are_visible():
    html = """
    <div>Indicative total tuition fee for domestic students $74,949.60</div>
    <div>Indicative total tuition fee for international students $251,437.94</div>
    """
    out = _run(fee.extract(html, "https://example.edu/course/x", country="Australia"))
    assert out
    assert out[0].normalized["international_fee"] == 251_437
    assert out[0].normalized["fee_term"] == "Full Course"


def test_unlabelled_repeat_of_domestic_amount_stays_rejected():
    """A later generic tuition section must retain the amount's known owner."""
    html = """
    <section>
      Indicative total tuition fee for domestic students $74,949.60
    </section>
    <section>
      Commonwealth Supported Places may be available.
      2027 Tuition Fee
      Indicative first-year tuition fee: $36,740.00
      Indicative total tuition fee: $74,949.60
    </section>
    """
    out = _run(
        fee.extract(
            html,
            "https://www.uts.edu.au/courses/master-of-philosophy-in-science",
            country="Australia",
        )
    )
    assert all(r.normalized["international_fee"] != 74_949 for r in out)


def test_full_course_fee_preferred_over_first_year_fee():
    """Murdoch-style pages show both 'First year fee: $41,990' and
    'Full course fee: $125,970'. The extractor must prefer the full-course
    total — picking the first-year sticker under-reports the programme cost
    by 3×."""
    html = (
        "<p>What type of student are you? International</p>"
        "<p>First year fee: A$41,990</p>"
        "<p>Full course fee: A$125,970</p>"
    )
    out = _run(fee.extract(html, "https://www.murdoch.edu.au/course/undergraduate/b1348"))
    assert out, "fee extractor must return a result"
    n = out[0].normalized
    assert n["international_fee"] == 125_970, (
        f"Expected full-course total $125,970, got {n['international_fee']}. "
        f"The 'Full course fee' label must outscore the 'First year fee' label."
    )
    assert n["fee_term"] == "Full Course"


def test_first_year_fee_only_still_extracted():
    """When ONLY a first-year fee is shown (no full-course total), the
    extractor should still return it (penalise, not disqualify)."""
    html = (
        "<p>International first year fee: A$38,000</p>"
    )
    out = _run(fee.extract(html, "https://example.edu/course/x"))
    assert out, "fee extractor must return a result when only first-year fee is present"
    n = out[0].normalized
    assert n["international_fee"] == 38_000


def test_audience_scoped_fee_prefers_international_year_one_when_configured():
    """Audience-tagged SSR cards must not leak the domestic or total amount."""
    html = """
    <div data-student-type="domestic">
      <dt>First year fee</dt><dd>$16,891</dd>
    </div>
    <div data-student-type="international" style="display:none">
      <dt>First year fee</dt><dd>$36,730</dd>
    </div>
    <div data-student-type="domestic">
      <dt>Full course fee</dt><dd>$67,564</dd>
    </div>
    <div data-student-type="international" style="display:none">
      <dt>Full course fee</dt><dd>$146,920</dd>
    </div>
    """
    cfg = UniConfig(
        slug="example",
        name="Example University",
        base_url="https://example.edu",
        scrape_url="https://example.edu/courses",
        extraction={"fees": {"prefer_year_one_over_total": True}},
    )
    token = current_uni_config.set(cfg)
    try:
        out = _run(
            fee.extract(
                html,
                "https://example.edu/course/b1362",
                country="Australia",
            )
        )
    finally:
        current_uni_config.reset(token)

    assert out
    assert out[0].normalized["international_fee"] == 36_730
    assert out[0].normalized["currency"] == "AUD"
    assert out[0].normalized["fee_term"] == "Annual"
    assert out[0].method == "fee.audience_structural"


def test_audience_scoped_fee_prefers_international_total_by_default():
    """The generic audience reader preserves the default total-fee policy."""
    html = """
    <div data-audience="domestic">
      <dt>First year fee</dt><dd>$16,891</dd>
    </div>
    <div data-audience="international">
      <dt>First year fee</dt><dd>$36,730</dd>
      <dt>Full course fee</dt><dd>$146,920</dd>
    </div>
    """
    cfg = UniConfig(
        slug="example",
        name="Example University",
        base_url="https://example.edu",
        scrape_url="https://example.edu/courses",
    )
    token = current_uni_config.set(cfg)
    try:
        out = _run(fee.extract(html, "https://example.edu/course/x"))
    finally:
        current_uni_config.reset(token)

    assert out
    assert out[0].normalized["international_fee"] == 146_920
    assert out[0].normalized["fee_term"] == "Full Course"
    assert out[0].method == "fee.audience_structural"


def test_audience_scoped_fee_rejects_ancillary_international_charges():
    """International audience containers also hold non-tuition charges."""
    html = """
    <div data-student-type="international">
      <dt>Application fee</dt><dd>$5,000</dd>
      <dt>Student services fee</dt><dd>$8,000</dd>
      <dt>Acceptance deposit</dt><dd>$12,000</dd>
    </div>
    """

    assert (
        fee._extract_audience_scoped_fee(html, prefer_year_one=True) is None
    )


def test_audience_scoped_fee_does_not_inherit_nested_domestic_card():
    """Nearest audience ownership prevents domestic values leaking upward."""
    html = """
    <section data-audience="international">
      <div data-audience="domestic">
        <dt>First year fee</dt><dd>$16,891</dd>
      </div>
      <div>
        <dt>Application fee</dt><dd>$5,000</dd>
      </div>
      <div>
        <dt>International tuition fee</dt><dd>$36,730</dd>
      </div>
    </section>
    """

    result = fee._extract_audience_scoped_fee(
        html, prefer_year_one=True
    )
    assert result is not None
    amount, _ctx = result
    assert amount == 36_730


def test_audience_scoped_fee_rejects_domestic_only_nested_wrapper():
    html = """
    <section data-audience="international">
      <div data-audience="domestic">
        <dt>First year fee</dt><dd>$16,891</dd>
      </div>
    </section>
    """

    assert (
        fee._extract_audience_scoped_fee(html, prefer_year_one=True) is None
    )


def test_audience_scoped_fee_rejects_mixed_owner_and_uses_intl_only_source():
    html = """
    <section data-audience="domestic international">
      <dt>First year fee</dt><dd>$16,891</dd>
    </section>
    <section data-audience="international">
      <dt>First year fee</dt><dd>$36,730</dd>
    </section>
    """

    result = fee._extract_audience_scoped_fee(
        html, prefer_year_one=True
    )
    assert result is not None
    amount, _ctx = result
    assert amount == 36_730


def test_audience_scoped_fee_rejects_mixed_owner_without_intl_only_source():
    html = """
    <section data-student-type="international domestic">
      <dt>Annual tuition fee</dt><dd>$16,891</dd>
    </section>
    """

    assert (
        fee._extract_audience_scoped_fee(html, prefer_year_one=True) is None
    )


def test_audience_scoped_fee_accepts_non_resident_as_international():
    html = """
    <section data-student-type="non-resident">
      <dt>Annual tuition fee</dt><dd>$42,500</dd>
    </section>
    """

    result = fee._extract_audience_scoped_fee(
        html, prefer_year_one=True
    )
    assert result is not None
    amount, _ctx = result
    assert amount == 42_500


def test_uow_session_fee_wins_over_full_course_fee():
    """UOW publishes the session amount beside the full-programme amount.

    The session fee is the operator-facing periodic tuition figure:
    $22,032/session, while $88,128 is the total course fee.  A flattened text
    scan used to choose the larger number and incorrectly label it Annual.
    """
    html = """
    <table>
      <thead>
        <tr>
          <th>Campus</th>
          <th>Delivery method</th>
          <th>Session fee*</th>
          <th>Course fee*</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>Wollongong</td>
          <td>On Campus</td>
          <td>$22032 (2026)</td>
          <td>$88128 (2026)</td>
        </tr>
      </tbody>
    </table>
    """
    out = _run(fee.extract(
        html,
        "https://www.uow.edu.au/study/courses/master-of-research-smah",
        country="Australia",
    ))
    assert out
    n = out[0].normalized
    assert n["international_fee"] == 22_032
    assert n["currency"] == "AUD"
    assert n["fee_term"] == "Session"
    assert n["fee_year"] == 2026
    assert out[0].method == "fee.uow_session_table"


def test_scu_international_snapshot_fee_beats_per_unit_rollup():
    """SCU's snapshot amount is annual, not a per-unit full-course total."""
    html = """
    <div data-course="international">
      <h3>International snapshot</h3>
      <p id="int_snapshot_fee">$26,000 (first year only)</p>
    </div>
    <table>
      <tr><td>$26,000 ($3,250 per unit)</td></tr>
    </table>
    <p>Equivalent units 32</p>
    <p>384 credit points</p>
    """
    out = _run(
        fee.extract(
            html,
            "https://www.scu.edu.au/study/courses/example/2026/",
        )
    )
    assert out
    assert out[0].normalized["international_fee"] == 26_000
    assert out[0].normalized["fee_term"] == "Annual"
    assert out[0].method == "fee.scu_int_snapshot"


def test_aut_international_panel_includes_levy_and_preserves_cents():
    html = """
    <div class="mb-small">
      <div class="mb-10">
        <div class="heading mb-5">Domestic</div>
        <div class="value">
          $11,851.60 (for 120 points)
          ($10,630 tuition fees + $1,221.60 student services levy)
        </div>
      </div>
    </div>
    <div class="mb-small">
      <div class="mb-10">
        <div class="heading mb-5">International</div>
        <div class="value">
          $42,859.67 (for 120 points)
          ($41,600 tuition fees + $1,259.67 student services levy)
        </div>
      </div>
    </div>
    """
    out = _run(
        fee.extract(
            html,
            "https://www.aut.ac.nz/study/study-options/example",
        )
    )
    assert out
    assert out[0].normalized["international_fee"] == 42_859.67
    assert out[0].normalized["currency"] == "NZD"
    assert out[0].normalized["fee_term"] == "Annual"
    assert out[0].method == "fee.aut_international_panel"


def test_aut_180_point_total_includes_levy_and_is_annualised():
    html = """
    <div class="mb-small">
      <div class="mb-10">
        <div class="heading mb-5">International</div>
        <div class="value">
          Not offered to new students in 2027
          $64,139.51 (for 180 points)
          ($62,250 tuition fees + $1,889.51 student services levy)
        </div>
      </div>
    </div>
    """
    out = _run(
        fee.extract(
            html,
            "https://www.aut.ac.nz/study/study-options/example",
        )
    )
    assert out
    assert out[0].normalized["international_fee"] == 42_759.67
    assert out[0].normalized["currency"] == "NZD"
    assert out[0].normalized["fee_term"] == "Annual"
    assert out[0].method == "fee.aut_international_panel"


# ── Structured fee table tests ─────────────────────────────────────────────
# These test the new _extract_fee_table_row pre-pass (Pre-pass 0) which must
# run before the label/keyword extractors so that UK Home / Part-time rows
# are never stored as the international annual fee.

def _uk_fee_table_html(rows: list[tuple[str, str, str, str]]) -> str:
    """Build a minimal HTML page containing a fee table matching the WLV
    pattern: Student type | Study mode | Fee | Year."""
    trs = "\n".join(
        f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td><td>{r[3]}</td></tr>"
        for r in rows
    )
    return f"<html><body><table>{trs}</table></body></html>"


_WLV_FULL_TABLE = _uk_fee_table_html([
    ("Home",          "Full time",  "£9,535 per year",  "2025 to 26"),
    ("Home",          "Full time",  "£9,790 per year",  "2026 to 27"),
    ("Home",          "Part time",  "£4,768 per year",  "2025 to 26"),
    ("Home",          "Part time",  "£4,895 per year",  "2026 to 27"),
    ("International", "Full time",  "£17,000 per year", "2025 to 26"),
    ("International", "Full time",  "£18,700 per year", "2026 to 27"),
])


def test_fee_table_picks_international_fulltime_latest_year():
    """The standard WLV six-row fee table must yield £18,700 (International,
    Full time, 2026 to 27) and reject all four Home rows."""
    out = _run(fee.extract(_WLV_FULL_TABLE, "https://www.wlv.ac.uk/course/x"))
    assert out, "fee table extractor must fire on a structured Home/International table"
    n = out[0].normalized
    assert n["international_fee"] == 18_700, (
        f"Expected £18,700 (Intl Full-time 2026/27), got {n['international_fee']}. "
        "Rejected values: 9535, 9790, 4768, 4895 (Home rows), 17000 (older year)."
    )
    assert n["currency"] == "GBP", f"Expected GBP, got {n['currency']}"
    assert n["fee_term"] == "Annual"
    assert out[0].method == "fee.table_row"


def test_fee_table_rejects_home_only_no_international_rows():
    """When the fee table has ONLY Home rows (e.g. HNC Building Studies,
    part-time only), the extractor must NOT store the Home fee as the
    international tuition. Instead it must return a definitive
    "fee_table_confirmed_no_international" signal so the pipeline can
    reject the course instead of fabricating an institutional default fee
    for it (a course this university does not actually offer to
    international students)."""
    html = _uk_fee_table_html([
        ("Home", "Full time",  "£9,790 per year", "2026 to 27"),
        ("Home", "Part time",  "£4,895 per year", "2026 to 27"),
    ])
    out = _run(fee.extract(html, "https://www.wlv.ac.uk/course/hnc-building"))
    assert len(out) == 1, f"Expected exactly one sentinel result, got: {out!r}"
    assert out[0].field_key == "fee_table_confirmed_no_international"
    assert out[0].normalized == {"fee_table_confirmed_no_international": True}
    assert not any(r.field_key == "international_fee" for r in out), (
        "A Home-only fee table must never produce an international_fee value. "
        f"Got: {out!r}"
    )


def test_fee_table_parttime_international_only_returns_no_intl_signal():
    """If International rows exist but ONLY for Part time (no Full-time
    International row), the extractor must not store a part-time rate as the
    annual international fee — it should surface the same definitive
    "no International + Full-time row" signal instead."""
    html = _uk_fee_table_html([
        ("Home",          "Full time",  "£9,790 per year", "2026 to 27"),
        ("International", "Part time",  "£9,000 per year", "2026 to 27"),
    ])
    out = _run(fee.extract(html, "https://www.wlv.ac.uk/course/pt-only"))
    assert len(out) == 1, f"Expected exactly one sentinel result, got: {out!r}"
    assert out[0].field_key == "fee_table_confirmed_no_international"
    assert not any(r.field_key == "international_fee" for r in out), (
        "International Part-time-only table must never produce an "
        f"international_fee value. Got: {out!r}"
    )


def test_fee_table_prefers_latest_year():
    """When two International + Full-time rows exist for different years,
    the extractor must return the higher-year (more recent) value."""
    html = _uk_fee_table_html([
        ("International", "Full time", "£17,000 per year", "2025 to 26"),
        ("International", "Full time", "£18,700 per year", "2026 to 27"),
    ])
    out = _run(fee.extract(html, "https://www.wlv.ac.uk/course/x"))
    assert out
    assert out[0].normalized["international_fee"] == 18_700


def test_generic_fee_candidates_prefer_latest_explicit_year_over_larger_old_fee():
    html = """
    <main>
      <p>2025 international tuition fee: AUD $48,000 per year.</p>
      <p>2027 international tuition fee: AUD $44,950 per year.</p>
    </main>
    """
    out = _run(fee.extract(html, "https://example.edu.au/course/science"))
    assert out
    assert out[0].normalized["international_fee"] == 44_950
    assert out[0].normalized["fee_year"] == 2027


def test_generic_fee_candidates_bind_year_printed_after_amount():
    html = """
    <main>
      <p>International tuition fee: AUD $48,000 per year for 2025.</p>
      <p>International tuition fee: AUD $44,950 per year for 2027.</p>
    </main>
    """
    out = _run(fee.extract(html, "https://example.edu.au/course/science"))
    assert out
    assert out[0].normalized["international_fee"] == 44_950
    assert out[0].normalized["fee_year"] == 2027


def test_structural_fee_pairs_still_choose_latest_explicit_year():
    html = """
    <dl>
      <dt>2025 international tuition fee</dt><dd>AUD $48,000 per year</dd>
      <dt>2027 international tuition fee</dt><dd>AUD $44,950 per year</dd>
    </dl>
    """
    out = _run(fee.extract(html, "https://example.edu.au/course/science"))
    assert out
    assert out[0].normalized["international_fee"] == 44_950
    assert out[0].normalized["fee_year"] == 2027
    assert out[0].method == "fee.latest_explicit_year"


def test_newer_dated_scholarship_never_overrides_international_tuition():
    html = """
    <main>
      <p>2027 international tuition fee: AUD $44,950 per year.</p>
      <p>2028 international tuition fee scholarship: AUD $2,000 per year.</p>
    </main>
    """
    out = _run(fee.extract(html, "https://example.edu.au/course/science"))
    assert out
    assert out[0].normalized["international_fee"] == 44_950
    assert out[0].normalized["fee_year"] == 2027


def test_une_travelling_scholarship_is_never_tuition():
    html = """
    <main>
      <p>Postgraduate (Coursework) Financial disadvantage Aboriginal or
      Torres Strait Islander Students</p>
      <h3>Dr Peter Hemphill Travelling Scholarship</h3>
      <p>Value (per annum) Up to $15,000 (for fees or travel costs,
      including living expenses)</p>
      <p>Study Type Full-time</p>
    </main>
    """

    out = _run(
        fee.extract(
            html,
            "https://www.une.edu.au/study/courses/bachelor-of-biomedical-science",
            country="Australia",
        )
    )

    assert out == []


def test_additional_pilot_licensing_cost_never_overrides_tuition():
    html = """
    <main>
      <p>International tuition fee £15,910 per year.</p>
      <p>To become airline-ready, students must also complete further licences
      or ratings such as CPL, MER, IR, UPRT, and APS MCC, which typically add
      another £50,000-£60,000 to the overall training cost.</p>
    </main>
    """

    out = _run(
        fee.extract(
            html,
            "https://www.bucks.ac.uk/courses/undergraduate/"
            "bsc-hons-aviation-management-commercial-pilot-training-helicopters",
            country="United Kingdom",
        )
    )

    assert out
    assert out[0].normalized["international_fee"] == 15_910


def test_additional_pilot_licensing_cost_alone_is_not_tuition():
    html = """
    <p>Students must complete further licences and ratings such as CPL, MER,
    IR, UPRT, and APS MCC, which add another £50,000-£60,000 to the overall
    training cost.</p>
    """

    out = _run(
        fee.extract(
            html,
            "https://www.bucks.ac.uk/courses/undergraduate/pilot-training",
            country="United Kingdom",
        )
    )

    assert out == []


def test_bucks_config_falls_back_to_published_undergraduate_tuition():
    from app.services.scraper.config.loader import load_uni_config

    cfg = load_uni_config(
        slug="bucks",
        scrape_url="https://www.bucks.ac.uk",
        university_id=None,
        name="Buckinghamshire New University",
    )

    assert cfg.extraction.fees.degree_level_defaults["undergraduate"] == 15_910


def test_csp_acronym_fee_is_never_international_tuition():
    html = """
    <main>
      <p>UNE has a cap on the number of CSP places that can be granted for
      certain course types and bands. See CSP availability.</p>
      <p>$14,721 estimated course fee per year if studying full-time.</p>
      <p>Estimated amenities fee per year $373.</p>
    </main>
    """

    out = _run(
        fee.extract(
            html,
            "https://www.une.edu.au/study/courses/example?international=true",
            country="Australia",
        )
    )

    assert out == []


def test_strict_context_keeps_explicit_international_tuition():
    cfg = UniConfig(
        slug="une",
        name="University of New England",
        base_url="https://www.une.edu.au",
        scrape_url="https://www.une.edu.au",
        extraction={
            "fees": {
                "require_explicit_international_context": True,
            }
        },
    )
    token = current_uni_config.set(cfg)
    try:
        domestic = _run(
            fee.extract(
                "<p>Estimated annual course fee AUD $14,721.</p>",
                "https://www.une.edu.au/study/courses/example",
                country="Australia",
            )
        )
        structured_domestic = _run(
            fee.extract(
                "<div><strong>Course fee</strong></div>"
                "<div>CSP estimated course fee: AUD $14,721 per year.</div>",
                "https://www.une.edu.au/study/courses/example",
                country="Australia",
            )
        )
        dated_structured_domestic = _run(
            fee.extract(
                "<dl>"
                "<dt>2026 International tuition fee</dt>"
                "<dd>CSP estimated course fee: AUD $13,000 per year.</dd>"
                "<dt>2027 International tuition fee</dt>"
                "<dd>CSP estimated course fee: AUD $14,721 per year.</dd>"
                "</dl>",
                "https://www.une.edu.au/study/courses/example",
                country="Australia",
            )
        )
        international = _run(
            fee.extract(
                "<p>International tuition fee AUD $36,800 per year.</p>",
                "https://www.une.edu.au/study/courses/example",
                country="Australia",
            )
        )
    finally:
        current_uni_config.reset(token)

    assert domestic == []
    assert structured_domestic == []
    assert dated_structured_domestic == []
    assert international
    assert international[0].normalized["international_fee"] == 36_800


def test_unpublished_new_year_does_not_attach_to_an_undated_fee():
    html = """
    <main>
      <p>2025 international tuition fee: AUD $40,000 per year.</p>
      <p>For 2027, international tuition fees will be announced shortly.</p>
      <p>International tuition fee: AUD $45,000 per year.</p>
    </main>
    """
    out = _run(fee.extract(html, "https://example.edu.au/course/science"))
    assert out
    assert out[0].normalized["international_fee"] == 40_000
    assert out[0].normalized["fee_year"] == 2025
    assert out[0].method != "fee.latest_explicit_year"


def test_fee_table_not_triggered_for_non_fee_tables():
    """An HTML page with a plain table (no Home/International rows) must
    NOT trigger the fee-table pre-pass — falls through to keyword extractor."""
    html = (
        "<table><tr><td>Module</td><td>Credits</td></tr>"
        "<tr><td>Core studies</td><td>30</td></tr></table>"
        "<p>International tuition fee: A$32,000 per year</p>"
    )
    out = _run(fee.extract(html, "https://uni.edu.au/course/x"))
    assert out, "keyword extractor must fire when fee table pre-pass does not match"
    assert out[0].normalized["international_fee"] == 32_000
