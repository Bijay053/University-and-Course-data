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

from app.services.scraper.extractors import fee


def _run(coro):
    return asyncio.run(coro)


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
