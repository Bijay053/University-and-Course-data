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
