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


@pytest.mark.asyncio
async def test_load_university_pdf_data_parses_fee(monkeypatch):
    async def fake_download(url):
        return _FEE_PDF_TEXT if "fee" in url else ""

    monkeypatch.setattr(university_pdfs, "download_pdf_text", fake_download)

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
    async def fake_download(url):
        return _REQ_PDF_TEXT if "req" in url or "policy" in url else ""

    monkeypatch.setattr(university_pdfs, "download_pdf_text", fake_download)

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
