"""Tests for the university-level PDF pipeline.

We never make real network calls in unit tests — the PDF text is
injected via monkeypatch on :func:`download_pdf_text`. Two scenarios:

1. Fee PDF parsed → fee/currency populated, course-level missing keys
   are backfilled, evidence rows credit ``method='uni_pdf:fees'``.
2. Requirements PDF parsed → IELTS overall + sub-bands populated.

Plus a regression test that ``extract_course`` does NOT touch keys
already present from page extraction (uni-PDF is last-resort only).
"""
from __future__ import annotations

import pytest

from app.services.scraper.pipelines import single_course, university_pdfs


_FEE_PDF_TEXT = """\
2026 International Fee Schedule

Bachelor of Business           AUD 24,000 per year
Master of Information Tech     AUD 28,500 per year
Diploma of Health              AUD 18,000 per year
"""

_REQ_PDF_TEXT = """\
Student Admissions Policy 2025.2

International applicants must demonstrate English proficiency.
Minimum requirements: IELTS Academic overall 6.0 with no band below 5.5.
Equivalent: PTE Academic 50, TOEFL iBT 60.
"""

_FEE_HTML_NOFEE = """\
<html><body>
<h1>Bachelor of Cybersecurity</h1>
<p>Duration: 3 years full time. Intake: February.</p>
<p>Apply now for the upcoming intake.</p>
</body></html>
"""


def _patch_pdf_pipeline(monkeypatch, fee_text="", req_text=""):
    """Bypass the real httpx + pypdf path. ``_download_raw_pdf`` returning
    a non-empty bytes value is enough to make the pipeline proceed; the
    text it would have extracted is injected via the PdfReader patch."""

    async def fake_download_raw(url):
        if "fee" in url:
            return b"%PDF-fake-fee-bytes" if fee_text else b""
        if "req" in url or "policy" in url:
            return b"%PDF-fake-req-bytes" if req_text else b""
        return b""

    class _FakePage:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class _FakeReader:
        def __init__(self, fp):
            data = fp.getvalue() if hasattr(fp, "getvalue") else fp
            if b"fee" in data:
                self.pages = [_FakePage(fee_text)]
            elif b"req" in data:
                self.pages = [_FakePage(req_text)]
            else:
                self.pages = []

    monkeypatch.setattr(university_pdfs, "_download_raw_pdf", fake_download_raw)
    # pypdf.PdfReader is imported INSIDE the parse functions, so patch the
    # real symbol — both _parse_fee_pdf and _parse_requirements_pdf pick
    # up the patched version on next import.
    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", _FakeReader)


@pytest.mark.asyncio
async def test_load_university_pdf_data_parses_fee(monkeypatch):
    _patch_pdf_pipeline(monkeypatch, fee_text=_FEE_PDF_TEXT)

    result = await university_pdfs.load_university_pdf_data(
        {"uniPages": {"feesPdf": "https://example.com/fees.pdf"}},
        country="Australia",
    )
    assert "fee" in result
    assert result["fees_pdf_url"] == "https://example.com/fees.pdf"
    fee = result["fee"]
    # Either 24000, 28500 or 18000 — the extractor picks one (highest score).
    assert fee.get("international_fee") in (24000, 28500, 18000)
    assert (fee.get("currency") or "").upper() in ("AUD", "AU$", "$", "A$")


@pytest.mark.asyncio
async def test_load_university_pdf_data_parses_ielts(monkeypatch):
    _patch_pdf_pipeline(monkeypatch, req_text=_REQ_PDF_TEXT)

    result = await university_pdfs.load_university_pdf_data(
        {"uniPages": {"requirementsPdf": "https://example.com/policy.pdf"}},
        country="Australia",
    )
    assert "english" in result
    assert result["requirements_pdf_url"] == "https://example.com/policy.pdf"
    eng = result["english"]
    assert eng.get("ielts_overall") == 6.0
    # Sub-band derived from "no band below 5.5"
    assert eng.get("ielts_listening") == 5.5
    assert eng.get("ielts_reading") == 5.5


@pytest.mark.asyncio
async def test_load_university_pdf_data_empty_when_no_config(monkeypatch):
    # No PDFs configured → nothing fetched, empty dict.
    called = []

    async def fake_download(url):
        called.append(url)
        return ""

    monkeypatch.setattr(university_pdfs, "download_pdf_text", fake_download)
    result = await university_pdfs.load_university_pdf_data(None, country="Australia")
    assert result == {}
    assert called == []

    result = await university_pdfs.load_university_pdf_data({}, country="Australia")
    assert result == {}
    assert called == []


@pytest.mark.asyncio
async def test_extract_course_backfills_from_uni_pdf(monkeypatch):
    """Course HTML has no fee/IELTS → uni-PDF data fills them."""
    uni_pdf_data = {
        "fee": {"international_fee": 24000, "currency": "AUD", "fee_term": "year"},
        "english": {"ielts_overall": 6.0, "ielts_listening": 5.5},
        "fees_pdf_url": "https://example.com/fees.pdf",
        "requirements_pdf_url": "https://example.com/policy.pdf",
    }

    out = await single_course.extract_course(
        url="https://uni.example.com/cyber",
        country="Australia",
        html=_FEE_HTML_NOFEE,
        use_ai_fallback=False,  # keep test offline
        uni_pdf_data=uni_pdf_data,
    )
    payload = out["payload"]
    # Page had no fee/ielts → uni-PDF backfill kicks in.
    assert payload.get("international_fee") == 24000
    assert payload.get("currency") == "AUD"
    assert payload.get("ielts_overall") == 6.0
    assert payload.get("ielts_listening") == 5.5

    # Provenance evidence rows must credit the PDFs.
    methods = {(e.get("field_key"), e.get("method")) for e in out["evidence"]}
    assert ("international_fee", "uni_pdf:fees") in methods
    assert ("ielts_overall", "uni_pdf:requirements") in methods


@pytest.mark.asyncio
async def test_extract_course_pdf_does_not_overwrite_existing(monkeypatch):
    """If the page already had a fee, uni-PDF must NOT overwrite it."""
    html_with_fee = """
    <html><body>
      <h1>Bachelor of Business</h1>
      <p>International tuition fee: AUD 32,000 per year.</p>
      <p>IELTS overall 6.5 with no band below 6.0.</p>
    </body></html>
    """
    uni_pdf_data = {
        "fee": {"international_fee": 24000, "currency": "AUD", "fee_term": "year"},
        "english": {"ielts_overall": 6.0},
        "fees_pdf_url": "https://example.com/fees.pdf",
        "requirements_pdf_url": "https://example.com/policy.pdf",
    }
    out = await single_course.extract_course(
        url="https://uni.example.com/biz",
        country="Australia",
        html=html_with_fee,
        use_ai_fallback=False,
        uni_pdf_data=uni_pdf_data,
    )
    payload = out["payload"]
    # Page values WIN, uni-PDF does not overwrite.
    assert payload.get("international_fee") == 32000
    assert payload.get("ielts_overall") == 6.5

    # No PDF-credited evidence row for these keys (since they were already filled).
    methods = {(e.get("field_key"), e.get("method")) for e in out["evidence"]}
    assert ("international_fee", "uni_pdf:fees") not in methods
    assert ("ielts_overall", "uni_pdf:requirements") not in methods


@pytest.mark.asyncio
async def test_extract_course_pdf_fills_through_empty_string_placeholder(
    monkeypatch,
):
    """Empty-aware precedence: if an upstream extractor writes a
    ``""``/``0`` placeholder into ``payload`` (key exists but holds an
    empty value), the uni-PDF backfill MUST still fill it.

    Pins down the harden in ``pipelines/single_course.py`` — the
    uni-PDF blocks now use the same
    ``payload.get(k) not in (None, "", 0)`` check that every other
    merge site uses (per-course modal, VIT static fallback, sibling
    cache). The previous ``k in payload`` check would have silently
    skipped this fill, leaving the placeholder in place.

    The test injects a placeholder by monkeypatching ``english_test``
    + ``fee`` extractors so step-1 of ``extract_course`` writes
    ``payload["ielts_overall"] = ""`` and
    ``payload["international_fee"] = 0`` via ``setdefault`` (step-1
    only filters ``None``, not empties — see single_course.py:148-154).
    Under the OLD ``k in payload`` check both keys would survive as
    placeholders; under the NEW empty-aware check the uni-PDF values
    take over.
    """
    from app.services.scraper.extractors import english_test, fee
    from app.services.scraper.extractors.base import ExtractionResult
    from app.services.scraper.pipelines.single_course import extract_course

    async def fake_english_extract(html, url):
        return [
            ExtractionResult(
                field_key="ielts_overall",
                value="",
                normalized={"ielts_overall": ""},
                confidence=0.1,
                method="test_placeholder",
                snippet="placeholder",
            )
        ]

    async def fake_fee_extract(html, url, country=None):
        return [
            ExtractionResult(
                field_key="international_fee",
                value=0,
                normalized={"international_fee": 0},
                confidence=0.1,
                method="test_placeholder",
                snippet="placeholder",
            )
        ]

    monkeypatch.setattr(english_test, "extract", fake_english_extract)
    monkeypatch.setattr(fee, "extract", fake_fee_extract)

    page_html = """
    <html><body>
      <h1>Master of Cybersecurity</h1>
      <p>Apply for the next intake.</p>
    </body></html>
    """
    uni_pdf_data = {
        "fee": {"international_fee": 30000, "currency": "AUD", "fee_term": "Annual"},
        "english": {"ielts_overall": 6.5, "ielts_listening": 6.0},
        "fees_pdf_url": "https://example.com/fees.pdf",
        "requirements_pdf_url": "https://example.com/policy.pdf",
    }

    out = await extract_course(
        url="https://uni.example.com/cyber-master",
        country="Australia",
        html=page_html,
        use_ai_fallback=False,
        uni_pdf_data=uni_pdf_data,
    )
    payload = out["payload"]
    # The placeholders ("" and 0) MUST have been overwritten by the
    # uni-PDF values — NOT preserved as empties.
    assert payload.get("ielts_overall") == 6.5, (
        f"empty-aware english-block fill failed; "
        f"got {payload.get('ielts_overall')!r} (placeholder survived)"
    )
    assert payload.get("international_fee") == 30000, (
        f"empty-aware fee-block fill failed; "
        f"got {payload.get('international_fee')!r} (placeholder survived)"
    )
    # Sub-band slot was never placeholdered → fills as before.
    assert payload.get("ielts_listening") == 6.0

    # Provenance must credit the PDFs for the fields that were
    # placeholder-overwritten — proves the uni-PDF block actually
    # ran for these keys (not just that the payload happened to
    # contain the right values for some other reason).
    methods = {(e.get("field_key"), e.get("method")) for e in out["evidence"]}
    assert ("ielts_overall", "uni_pdf:requirements") in methods
    assert ("international_fee", "uni_pdf:fees") in methods


@pytest.mark.asyncio
async def test_uni_pdf_backfill_is_applied_before_sibling_cache():
    """Pin down the current backfill ORDERING between uni-PDF and
    sibling-cache.

    Today's pipeline sequence (orchestrator.run_scrape → _extract_only →
    extract_course → backfill_english_from_siblings):

    1. ``extract_course`` runs all per-page extractors AND the uni-PDF
       backfill before returning. Empty slots are populated by uni-PDF
       at this point.
    2. After all per-URL extractions complete, the orchestrator calls
       ``backfill_english_from_siblings`` over the full result set.
       Sibling-cache only fills slots that are still empty
       (``payload.get(k) not in (None, "", 0)``).

    Consequence (architect-flagged): when one sibling course in a
    bucket has a real extracted English value (e.g. 6.5) and another
    sibling has only the generic uni-PDF value (e.g. 6.0), the
    second sibling KEEPS the uni-PDF value — sibling-cache cannot
    supply the per-cohort peer extraction because the slot is
    already non-empty.

    This test pins the current behaviour. It is intentionally a
    "current state" lock — any future change to make sibling-cache
    beat uni-PDF should be a deliberate, documented product decision
    that flips this assertion (see follow-up task on uni-PDF vs
    sibling precedence policy).
    """
    from app.services.scraper.sibling_cache import (
        backfill_english_from_siblings,
    )

    # Two postgrad sibling courses:
    # - course_x: per-page extractor genuinely succeeded (ielts_overall=6.5)
    # - course_y: per-page extractor empty, uni-PDF then filled 6.0
    results = [
        {
            "url": "https://uni.example.com/master-x",
            "payload": {
                "course_name": "Master of X",
                "degree_level": "Master",
                "ielts_overall": 6.5,  # real per-page extraction
            },
            "evidence": [],
        },
        {
            "url": "https://uni.example.com/master-y",
            "payload": {
                "course_name": "Master of Y",
                "degree_level": "Master",
                "ielts_overall": 6.0,  # uni-PDF backfill (generic value)
            },
            "evidence": [],
        },
    ]
    fills = await backfill_english_from_siblings(results)

    # Sibling-cache observes both buckets as already-filled and does
    # NOT override. Master Y keeps the uni-PDF generic value.
    assert fills == 0, (
        f"sibling-cache should NOT override an existing (uni-PDF) value "
        f"under the current ordering; got {fills} fills"
    )
    assert results[0]["payload"]["ielts_overall"] == 6.5
    assert results[1]["payload"]["ielts_overall"] == 6.0, (
        "PINNING: under current ordering, course Y keeps the uni-PDF "
        "value 6.0 even though sibling X extracted 6.5 from its page. "
        "Flipping this assertion requires a deliberate product decision "
        "to reorder uni-PDF after sibling-cache (see replit.md)."
    )


@pytest.mark.asyncio
async def test_vision_not_invoked_when_text_extraction_succeeds(monkeypatch):
    """Regression: vision OCR is the LAST resort. If text extraction
    yields a usable fee/IELTS payload, ``extract_via_vision`` must NOT
    be called — it costs Gemini quota and slows the pipeline.
    """
    _patch_pdf_pipeline(monkeypatch, fee_text=_FEE_PDF_TEXT, req_text=_REQ_PDF_TEXT)

    vision_calls: list[bytes] = []

    async def spy_vision(pdf_bytes, **_kw):
        vision_calls.append(pdf_bytes)
        return "should not appear"

    # Patch in both modules — pipeline imports it lazily but the symbol
    # is bound at the module that owns the function.
    import app.services.scraper.pdf_vision as pv
    monkeypatch.setattr(pv, "extract_via_vision", spy_vision)

    out = await university_pdfs.load_university_pdf_data(
        {
            "uniPages": {
                "feesPdf": "https://example.com/fees.pdf",
                "requirementsPdf": "https://example.com/policy.pdf",
            }
        },
        country="Australia",
    )
    # Both extractors produced data from text, so vision must not have run.
    assert "fee" in out
    assert "english" in out
    assert vision_calls == [], (
        "vision OCR was invoked even though text extraction succeeded — "
        "this would silently double Gemini cost on every healthy PDF"
    )
