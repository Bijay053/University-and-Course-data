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


# ---------- per-course PDF table parsing ----------------------------------
# Real ASA "2026 International Student Tuition Fee Schedule" excerpt. We
# keep this verbatim (with the original line breaks) so the parser is
# exercised against the same shape pypdf actually produces in prod —
# multi-line names, inline CRICOS codes, "Including Majors:" sub-lists,
# the "Undergraduate"/"Postgraduate" section dividers, and the trailing
# footnote markers.
_ASA_PDF_EXCERPT = """\
2026 International Student
Tuition Fee Schedule
All prices are indicative. 2026 Total course pricing will be subject to units enrolled.
Course Course
CRICOS
code
Course
Duration
Full Time
(Years)
Number
of Units
Fee per
unit
Annual
2026 Fee
Total
Course
Fee
Undergraduate
Bachelor of Professional
Accounting
102219K 3 24 $2,420 $19,360 $58,080
Bachelor of Business

Including Majors:
Technology Management
International Business
Hospitality Management
108859G 3 23* $2,420 $19,360 $58,080
Postgraduate
Master of Information Technology
(Cyber Security)
117597E 2 15* $3,300 $26,400 $52,800
Master of Project Management 117606J 2 15* $3,300 $26,400 $52,800
Master of Software Application Design 117603A 2 15* $3,300 $26,400 $52,800
*Final unit within the course is worth 20 credit points.
"""


def test_pick_per_course_amounts_parses_asa_table():
    """Per-course parser yields ≥2 distinct fees from a real fee schedule."""
    rows = university_pdfs._pick_per_course_amounts(_ASA_PDF_EXCERPT)
    # 5 unique CRICOS codes in the excerpt → 5 rows.
    assert len(rows) == 5, f"expected 5 per-course rows, got {len(rows)}: {rows}"

    cricos_to_fee = {r["_cricos"]: r["international_fee"] for r in rows.values()}
    assert cricos_to_fee == {
        "102219K": 58080,  # Bachelor of Professional Accounting
        "108859G": 58080,  # Bachelor of Business
        "117597E": 52800,  # Master of IT (Cyber Security)
        "117606J": 52800,  # Master of Project Management
        "117603A": 52800,  # Master of Software Application Design
    }
    # All rows tagged Full Course (total > annual).
    assert {r["fee_term"] for r in rows.values()} == {"Full Course"}
    # Section divider "Undergraduate" / "Postgraduate" must NOT leak
    # into any primary name.
    for r in rows.values():
        assert "undergraduate" not in r["_pdf_primary_name"].lower()
        assert "postgraduate" not in r["_pdf_primary_name"].lower()
    # Multi-line name was joined.
    professional = next(r for r in rows.values() if r["_cricos"] == "102219K")
    assert professional["_pdf_primary_name"] == "Bachelor of Professional Accounting"
    # Parenthetical continuation folded into primary name.
    cyber = next(r for r in rows.values() if r["_cricos"] == "117597E")
    assert "(Cyber Security)" in cyber["_pdf_primary_name"]


def test_match_course_in_pdf_table_matches_variant_db_names():
    """Matcher: 'Bachelor of Business Hospitality Management' (DB) →
    'Bachelor of Business' parent row (PDF) via the Including-Majors
    sub-list. 'Master of IT (Software App Development)' (DB) →
    'Master of Software Application Design' (PDF) via pdf-coverage scoring."""
    rows = university_pdfs._pick_per_course_amounts(_ASA_PDF_EXCERPT)

    cases = {
        "Bachelor of Professional Accounting": ("102219K", 58080),
        "Bachelor of Business": ("108859G", 58080),
        "Bachelor of Business Hospitality Management": ("108859G", 58080),
        "Bachelor of Business International Business": ("108859G", 58080),
        "Bachelor of Business Technology Management": ("108859G", 58080),
        "Master of Information Technology (Cyber Security)": ("117597E", 52800),
        "Master of Project Management": ("117606J", 52800),
        # Source-data inconsistency: PDF spells it "Design" but the
        # university website (and DB) spells it "Development". The
        # symmetric scoring in match_course_in_pdf_table picks the
        # right row anyway — its three tokens (software, application,
        # design) are nearly fully covered by the DB name.
        "Master of Information Technology (Software Application Development)": (
            "117603A",
            52800,
        ),
    }
    for db_name, (expected_cricos, expected_fee) in cases.items():
        matched, _suffix = university_pdfs.match_course_in_pdf_table(db_name, rows)
        assert matched is not None, f"no PDF row matched DB course {db_name!r}"
        assert matched["international_fee"] == expected_fee, (
            f"{db_name!r}: expected ${expected_fee}, got "
            f"${matched['international_fee']}"
        )
        # Public matcher result must NOT leak the private match-helper fields.
        for private_key in ("_pdf_primary_name", "_pdf_match_text", "_cricos"):
            assert private_key not in matched, (
                f"matcher leaked private field {private_key!r} to caller"
            )
        # Fuzzy path suffix: no CRICOS supplied, so must be name_match.
        assert _suffix == "name_match", (
            f"expected name_match suffix for {db_name!r}, got {_suffix!r}"
        )

    # Sanity: the 4 bachelor variants and 4 master courses produce ≥2
    # distinct fees — proves the per-course path is *differentiating*,
    # not just stamping the same value everywhere.
    fees = {
        university_pdfs.match_course_in_pdf_table(name, rows)[0]["international_fee"]
        for name in cases
    }
    assert len(fees) >= 2, (
        f"per-course path must produce at least 2 distinct fees; got {fees}"
    )


def test_match_course_in_pdf_table_returns_none_for_unrelated_course():
    """Matcher: a course with no overlapping distinctive tokens returns
    None so the caller falls back to the uni-wide value rather than
    cross-polluting fees between unrelated courses."""
    rows = university_pdfs._pick_per_course_amounts(_ASA_PDF_EXCERPT)
    # No business/IT tokens — should NOT match anything in the ASA table.
    matched_nursing, _s1 = university_pdfs.match_course_in_pdf_table(
        "Diploma of Nursing", rows
    )
    assert matched_nursing is None
    assert _s1 == "no_match"
    matched_vet, _s2 = university_pdfs.match_course_in_pdf_table(
        "Bachelor of Veterinary Science", rows
    )
    assert matched_vet is None
    assert _s2 == "no_match"


@pytest.mark.asyncio
async def test_load_university_pdf_data_surfaces_fee_by_course(monkeypatch):
    """Top-level loader exposes ``fee_by_course`` when the PDF is a
    multi-row schedule, alongside the existing uni-wide ``fee`` block."""
    _patch_pdf_pipeline(monkeypatch, fee_text=_ASA_PDF_EXCERPT)

    result = await university_pdfs.load_university_pdf_data(
        {"uniPages": {"feesPdf": "https://example.com/fees.pdf"}},
        country="Australia",
    )
    assert "fee_by_course" in result, (
        f"loader should surface per-course rows; got keys {list(result)}"
    )
    by_course = result["fee_by_course"]
    assert len(by_course) == 5
    # Uni-wide fee block is still present (the picker fallback) — it
    # provides the value for any course whose name doesn't match a row.
    assert "fee" in result
    # Per-course payload must NOT leak into the uni-wide block.
    assert "_by_course" not in result["fee"]
    assert result["fees_pdf_url"] == "https://example.com/fees.pdf"


@pytest.mark.asyncio
async def test_extract_course_uses_per_course_pdf_row_over_uni_wide(monkeypatch):
    """End-to-end merge: when ``fee_by_course`` includes a row matching
    the course name, the per-course value beats the uni-wide stamp AND
    the provenance method is tagged ``uni_pdf:fees:per_course``."""
    uni_pdf_data = {
        "fee": {"international_fee": 58080, "currency": "AUD", "fee_term": "Annual"},
        "fee_by_course": {
            # Master of IT (Cyber Security) — $52,800
            "cyber information security technology": {
                "international_fee": 52800,
                "currency": "AUD",
                "fee_term": "Full Course",
                "fee_year": 2026,
                "_pdf_match_text": "Master of Information Technology (Cyber Security)",
                "_pdf_primary_name": "Master of Information Technology (Cyber Security)",
                "_cricos": "117597E",
            },
            # Bachelor of Business — $58,080
            "business": {
                "international_fee": 58080,
                "currency": "AUD",
                "fee_term": "Full Course",
                "fee_year": 2026,
                "_pdf_match_text": (
                    "Bachelor of Business Including Majors: "
                    "Technology Management International Business "
                    "Hospitality Management"
                ),
                "_pdf_primary_name": "Bachelor of Business",
                "_cricos": "108859G",
            },
        },
        "fees_pdf_url": "https://example.com/fees.pdf",
    }
    page_html = """
    <html><body>
      <h1>Master of Information Technology (Cyber Security)</h1>
      <p>Apply for the next intake.</p>
    </body></html>
    """
    out = await single_course.extract_course(
        url="https://uni.example.com/cyber",
        country="Australia",
        html=page_html,
        use_ai_fallback=False,
        uni_pdf_data=uni_pdf_data,
    )
    payload = out["payload"]
    # Per-course row WINS over the uni-wide $58,080 stamp.
    assert payload.get("international_fee") == 52800, (
        f"per-course PDF row should beat uni-wide stamp; "
        f"got ${payload.get('international_fee')}"
    )
    assert payload.get("fee_term") == "Full Course"

    # Provenance must distinguish per-course rows from the old uni-wide
    # method so reviewers can tell them apart in the dashboard.
    methods = {(e.get("field_key"), e.get("method")) for e in out["evidence"]}
    assert ("international_fee", "uni_pdf:fees:per_course") in methods, (
        f"per-course evidence row not emitted; got methods {methods}"
    )
    # Old uni-wide method must NOT also be emitted for this field.
    assert ("international_fee", "uni_pdf:fees") not in methods


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


# ----------------------------------------------------------------------
# PR-7 review regressions — both bugs found in code review.
# ----------------------------------------------------------------------

# Synthesised text shaped like a fee-schedule PDF that mixes award levels
# under the same stem (Public Health). Used to prove:
#   1) the parser keeps the Cert and the Master as SEPARATE entries
#      (keyed by CRICOS), instead of collapsing them onto one
#      token-set key and silently dropping the lower fee.
#   2) the matcher uses degree-level info to pick the right one.
_PUBLIC_HEALTH_PDF = """
Tuition Fee Schedule 2026
International Students

Postgraduate
Course Name                                            CRICOS    Yrs Units Per-Unit  Annual    Total

Graduate Certificate of Public Health                  200001A   1   4    $4,000   $16,000   $16,000
Master of Public Health                                200002B   2   16   $4,000   $32,000   $64,000
"""

# Synthesised text used to prove the matcher rejects single-token
# false positives. Two separate CRICOS rows: a generic short title
# ("Master of Design") and a longer related title that contains
# "design" plus other tokens. Querying the long row's name should
# match the long row, NOT the short one — even though "design" is
# a 100% pdf-coverage hit against the short row.
_DESIGN_PDF = """
Tuition Fee Schedule 2026

Postgraduate
Course Name                                 CRICOS    Yrs Units Per-Unit  Annual    Total

Master of Design                            300001A   2   16   $3,000   $24,000   $48,000

Undergraduate
Bachelor of Interior Design                 300002B   3   24   $2,500   $20,000   $60,000
Residential
"""


def test_pick_per_course_keys_by_cricos_not_token_set():
    """REGRESSION (PR-7 review #1): Certificate and Master that share
    the same stem ("Public Health") tokenise to identical token sets
    after stopword stripping. Keying the parser output by token set
    collapses them into one entry and silently drops the cheaper
    fee — Torrens-class data loss. CRICOS is the only safe key.
    """
    rows = university_pdfs._pick_per_course_amounts(_PUBLIC_HEALTH_PDF)
    assert "200001A" in rows, f"Cert row was dropped! got {list(rows)}"
    assert "200002B" in rows, f"Master row was dropped! got {list(rows)}"
    # Each award level must keep ITS OWN fee.
    assert rows["200001A"]["international_fee"] == 16000, (
        "Cert lost its fee — token-set collision regression"
    )
    assert rows["200002B"]["international_fee"] == 64000


def test_match_avoids_award_level_collision():
    """REGRESSION (PR-7 review #1, downstream): even with both CRICOS
    rows preserved, the matcher must pick the correct award level —
    'Master of Public Health' must NOT match the Cert row, and the
    Cert query must NOT match the Master row.
    """
    rows = university_pdfs._pick_per_course_amounts(_PUBLIC_HEALTH_PDF)

    master, _sm = university_pdfs.match_course_in_pdf_table(
        "Master of Public Health", rows
    )
    assert master is not None and master["international_fee"] == 64000, (
        f"Master query mismatched — got {master}"
    )
    assert _sm == "name_match"

    cert, _sc = university_pdfs.match_course_in_pdf_table(
        "Graduate Certificate of Public Health", rows
    )
    assert cert is not None and cert["international_fee"] == 16000, (
        f"Cert query mismatched — got {cert}"
    )
    assert _sc == "name_match"

    # Sanity: querying for an absent level falls through cleanly.
    diploma, _sd = university_pdfs.match_course_in_pdf_table(
        "Diploma of Public Health", rows
    )
    assert diploma is None, (
        f"No Diploma row exists; matcher must return None, not "
        f"cross-pollute from Cert/Master. got {diploma}"
    )
    assert _sd == "no_match"


def test_match_rejects_short_token_false_positive():
    """REGRESSION (PR-7 review #2): the old matcher used
    ``max(overlap/db, overlap/pdf)`` with no minimum-overlap floor.
    A 1-token PDF row like 'Master of Design' (tokens={design})
    scored 1.0 against any DB course containing 'design', causing
    cross-course fee contamination — the exact failure mode this
    whole PR is supposed to prevent. The fix: require ≥2 shared
    tokens unless DB and PDF *primary* names tokenize identically.
    """
    rows = university_pdfs._pick_per_course_amounts(_DESIGN_PDF)
    assert "300001A" in rows and "300002B" in rows, (
        f"design PDF parsing setup failed: {list(rows)}"
    )

    # The long DB course shares only "design" with the short PDF row,
    # but matches all 3 distinctive tokens of its OWN PDF row. The
    # old scorer would have picked the short row (1.0 pdf-coverage).
    matched, _s1 = university_pdfs.match_course_in_pdf_table(
        "Bachelor of Interior Design Residential", rows
    )
    assert matched is not None, "valid long-name match should still work"
    assert matched["international_fee"] == 60000, (
        f"matcher cross-polluted from 'Master of Design' row — "
        f"got ${matched['international_fee']} (Master price), expected "
        f"$60000 (Bachelor price)"
    )

    # Another flavour of the same false positive — different DB course
    # also containing only 'design' as the shared token.
    matched2, _s2 = university_pdfs.match_course_in_pdf_table(
        "Bachelor of Game Design and Development", rows
    )
    # Should NOT match anything: doesn't tokenize identically to
    # 'Master of Design', and overlaps the Bachelor row only on
    # 'design' (1 token, below floor).
    assert matched2 is None, (
        f"Master-of-Design row falsely matched 'Game Design and "
        f"Development' — got {matched2}"
    )
    assert _s2 == "no_match"


def test_match_short_legitimate_exact_set():
    """ESCAPE HATCH for the floor: when a real DB course tokenises to
    exactly the same set as a PDF row's primary name (e.g. both
    reduce to ``{design}``), the matcher must still match. Otherwise
    parent rows like 'Bachelor of Business' would be unreachable.
    """
    rows = university_pdfs._pick_per_course_amounts(_DESIGN_PDF)
    matched, _sm = university_pdfs.match_course_in_pdf_table(
        "Master of Design", rows
    )
    assert matched is not None and matched["international_fee"] == 48000, (
        f"exact-set escape hatch broken: 'Master of Design' query "
        f"should match its own row; got {matched}"
    )
    assert _sm == "name_match"


def test_degree_level_helper():
    """Sanity-check the canonicalisation helper that powers the
    award-level filter. Order in _DEGREE_LEVEL_PREFIXES matters —
    multi-word levels must be tested before their substrings.
    """
    cases = {
        "Master of Public Health": "master",
        "Bachelor of Business": "bachelor",
        "Bachelor of Science (Honours)": "bachelor",
        "Graduate Certificate of Public Health": "graduate-certificate",
        "Graduate Diploma of Education": "graduate-diploma",
        "Postgraduate Certificate of Nursing": "graduate-certificate",
        "Diploma of Nursing": "diploma",
        "Certificate IV in Hospitality": "certificate",
        "Doctor of Philosophy": "doctor",
        "Doctorate of Business Administration": "doctor",
        "Associate Degree in Engineering": "associate",
        "Foundation Studies": "",
        "": "",
    }
    for name, expected in cases.items():
        got = university_pdfs._degree_level(name)
        assert got == expected, (
            f"_degree_level({name!r}) = {got!r}, expected {expected!r}"
        )


# ---------------------------------------------------------------------------
# Torrens regressions (uni id=22)
# ---------------------------------------------------------------------------
#
# Symptoms reported by the user: Torrens dashboard showed ~60 courses with
# every fee stamped at $121,955 (the highest number found anywhere in the
# fee schedule PDF). Root causes:
#
#   1. ``_strip_campus_tail`` was missing — Torrens' fee schedule appends a
#      campus list ("Sydney, Melbourne, Online") to course names, which
#      drowned out the discriminative tokens during matching.
#   2. The data-row regex required INTEGER durations and CANONICAL 6+letter
#      CRICOS codes. Torrens postgrad uses half-year increments (0.5, 1.5,
#      1.7) and at least one 7-digit CRICOS (0101388). Rows that didn't
#      match the regex were silently swallowed as continuation text into
#      the previous row's primary name, producing massive polluted
#      primaries like "Master of … (Advanced) Adelaide, Brisbane, … 0101388
#      2 16 $5,156 $41,250 $82,500 4 2026 International Student …".
#   3. The matcher unioned primary tokens with extras (`_pdf_match_text`)
#      with no upper bound. When extras was a 130-token blob of footer
#      text, short generic queries like "Higher Degrees By Research" got
#      false-positive matches via coincidental {higher, by} overlap.
#   4. The merge step fell back to the uni-wide stamp ($121,955 for
#      Torrens) whenever the per-course matcher returned None, recreating
#      the original "every course gets the same fee" failure mode this
#      whole project exists to fix.
#
# Fixes are tested below.


def test_strip_campus_tail_handles_torrens_layout():
    """Torrens fee schedule appends a comma-separated campus list to the
    primary course name (e.g. "Bachelor of Business (Accounting) Sydney,
    Melbourne, Online"). _strip_campus_tail must remove that tail
    without touching legitimate words elsewhere in the name.
    """
    cases = {
        "Bachelor of Business (Accounting) Sydney, Melbourne, Online":
            "Bachelor of Business (Accounting)",
        "Master of Public Health Sydney":
            "Master of Public Health",
        "Bachelor of Information Technology Adelaide, Brisbane, Melbourne, Sydney, Online":
            "Bachelor of Information Technology",
        # Unchanged when no campus tail is present:
        "Bachelor of Professional Accounting":
            "Bachelor of Professional Accounting",
        # Doesn't strip mid-name capitalised tokens — only the trailing run:
        "Master of Sydney Studies":
            "Master of Sydney Studies",
        "":
            "",
    }
    for raw, expected in cases.items():
        got = university_pdfs._strip_campus_tail(raw)
        assert got == expected, (
            f"_strip_campus_tail({raw!r}) = {got!r}, expected {expected!r}"
        )


def test_pick_per_course_amounts_matches_decimal_duration_rows():
    """REGRESSION: Torrens postgrad rows use half-year durations
    (0.5, 1.5, 1.7). The old data-row regex required ``\\d+`` so these
    rows were silently dropped and got swallowed as continuation text
    into the previous row's primary name, polluting the matcher and
    leaving real per-course fees invisible.
    """
    pdf = (
        "Postgraduate\n"
        "Master of Business Administration Sydney, Melbourne, Online "
        "095353M 1.5 12 $4,500 $36,000 $54,000\n"
        "Graduate Certificate of Cybersecurity Adelaide, Brisbane "
        "110794A 0.5 4 $4,975 $19,900 $19,900\n"
        "Master of Cybersecurity Adelaide "
        "110792C 1.7 11 $4,975 $39,800 $59,700\n"
    )
    rows = university_pdfs._pick_per_course_amounts(pdf)
    assert "095353M" in rows, (
        f"1.5-year MBA row not parsed (decimal duration regression): "
        f"{list(rows)}"
    )
    assert "110794A" in rows, (
        f"0.5-year cert row not parsed: {list(rows)}"
    )
    assert "110792C" in rows, (
        f"1.7-year master row not parsed: {list(rows)}"
    )
    # And the campus tail must be stripped from each primary:
    assert (
        rows["095353M"]["_pdf_primary_name"]
        == "Master of Business Administration"
    ), rows["095353M"]["_pdf_primary_name"]


def test_pick_per_course_amounts_matches_seven_digit_cricos():
    """REGRESSION: Torrens 2026 schedule has at least one 7-digit CRICOS
    (``0101388``). The old regex only accepted ``\\d{6}[A-Z]`` so this
    row was dropped, and its content got swallowed into the previous
    row's primary name as a giant pollution source.
    """
    # Anchor with one canonical 6+letter row so the parser has at least
    # two qualifying rows to walk between (matches the production PDF
    # shape where data rows always appear in groups).
    pdf = (
        "Postgraduate\n"
        "Bachelor of Business Sydney, Online 094008C 3 24 $3,950 $31,600 $94,800\n"
        "Master of Business Administration (Sport Management) (Advanced) "
        "Sydney, Online 0101388 2 16 $5,156 $41,250 $82,500\n"
        "Master of Public Health Sydney 097404M 1 12 $3,975 $31,800 $47,700\n"
    )
    rows = university_pdfs._pick_per_course_amounts(pdf)
    assert "0101388" in rows, (
        f"7-digit CRICOS row not parsed: {list(rows)}"
    )
    row = rows["0101388"]
    assert row["international_fee"] == 82500, row
    assert (
        row["_pdf_primary_name"]
        == "Master of Business Administration (Sport Management) (Advanced)"
    ), row["_pdf_primary_name"]


def test_match_rejects_polluted_extras_blob():
    """REGRESSION: when the parser swallows footer text into a row's
    extras blob, the matcher's primary∪extras union balloons to >100
    tokens. Short generic queries then false-positive match on
    coincidental token overlap (e.g. "Higher Degrees By Research"
    matching "Master of Sport Management" via {higher, by}).

    The fix bounds extras at 25 tokens; anything larger is treated as
    parser pollution and the matcher falls back to primary-only.
    """
    polluted_blob = " ".join(
        f"noiseword{i}" for i in range(50)
    ) + " higher by"
    by_course = {
        "999999A": {
            "international_fee": 99999,
            "fee_term": "Full Course",
            "fee_year": 2026,
            "currency": "AUD",
            "_pdf_primary_name": "Master of Sport Management",
            "_pdf_match_text": (
                "Master of Sport Management " + polluted_blob
            ),
            "_cricos": "999999A",
        }
    }
    matched, _sp = university_pdfs.match_course_in_pdf_table(
        "Higher Degrees By Research", by_course
    )
    assert matched is None, (
        f"polluted-extras false positive returned: {matched}"
    )
    assert _sp == "no_match"

    # Sanity: the legitimate match for the actual primary still works.
    matched_real, _sr = university_pdfs.match_course_in_pdf_table(
        "Master of Sport Management", by_course
    )
    assert matched_real is not None, (
        "primary-only match must still fire after extras-cap defense"
    )
    assert matched_real["international_fee"] == 99999
    assert _sr == "name_match"


@pytest.mark.asyncio
async def test_per_course_path_suppresses_uni_wide_stamp_with_two_row_table(
    monkeypatch,
):
    """REGRESSION: when the schedule PDF parses to ≥2 per-course rows
    (the same threshold ``_pick_per_course_amounts`` uses to consider
    the table "real"), an unmatched course must NOT inherit the
    uni-wide stamp — that re-creates the original Torrens v1 failure
    mode (every course gets the same number).

    The single_course merge gates this on ``len(fee_by_course) >= 2``.
    Earlier the threshold was 3, which silently mis-stamped tiny
    schedules (architect review caught this).
    """
    uni_pdf_data = {
        "fee": {
            "international_fee": 121955,
            "currency": "AUD",
            "fee_term": "Annual",
        },
        # Exactly 2 per-course rows — the smallest "real" table.
        "fee_by_course": {
            "111111A": {
                "international_fee": 30000,
                "currency": "AUD",
                "fee_term": "Full Course",
                "fee_year": 2026,
                "_pdf_match_text": "Bachelor of Business",
                "_pdf_primary_name": "Bachelor of Business",
                "_cricos": "111111A",
            },
            "222222B": {
                "international_fee": 40000,
                "currency": "AUD",
                "fee_term": "Full Course",
                "fee_year": 2026,
                "_pdf_match_text": "Master of Business Administration",
                "_pdf_primary_name": "Master of Business Administration",
                "_cricos": "222222B",
            },
        },
        "fees_pdf_url": "https://example.com/fees.pdf",
    }
    # Course is genuinely not in the schedule — must NOT inherit the
    # $121,955 uni-wide stamp; should land NULL instead.
    page_html = """
    <html><body>
      <h1>Doctor of Philosophy in Astrophysics</h1>
      <p>Research degree.</p>
    </body></html>
    """
    out = await single_course.extract_course(
        url="https://uni.example.com/phd-astro",
        country="Australia",
        html=page_html,
        use_ai_fallback=False,
        uni_pdf_data=uni_pdf_data,
    )
    payload = out["payload"]
    assert payload.get("international_fee") in (None, 0, "", "null"), (
        f"unmatched course must not inherit uni-wide stamp when per-course "
        f"path is active (≥2 rows); got ${payload.get('international_fee')}"
    )
    # And the uni-wide-stamp evidence row must NOT be emitted either —
    # otherwise downstream merges could resurrect the wrong number.
    methods = {(e.get("field_key"), e.get("method")) for e in out["evidence"]}
    assert ("international_fee", "uni_pdf:fees") not in methods, (
        f"uni-wide fee evidence leaked despite per-course path active; "
        f"methods={methods}"
    )
