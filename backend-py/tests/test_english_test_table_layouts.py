"""Bug G: English-test extractor must handle PDF table layouts.

Real requirements PDFs (e.g. ASA's "International Student English
Requirements 2025") flatten into whitespace-runs after pypdf
extraction, so a table row like::

    PTE Academic | 50 | 36

becomes::

    PTE Academic   50   36

The previous regexes only matched the prose form ("PTE Academic 50
with no skill below 36") and so prod scrapes captured IELTS but
silently dropped PTE/TOEFL/CAE/Duolingo. This test pins the table
fallbacks added in extractors/english_test.py.
"""
from __future__ import annotations

from app.services.scraper.extractors import english_test as et


def test_pte_table_layout():
    txt = "PTE Academic   50   36"
    out = et._pte(txt)
    assert out is not None
    assert out["overall"] == 50
    assert out["listening"] == 36


def test_pte_pipe_separated():
    txt = "PTE Academic | 50 | 36"
    out = et._pte(txt)
    assert out is not None
    assert out["overall"] == 50
    assert out["listening"] == 36


def test_pte_rich_form_still_works():
    txt = "PTE Academic 58 with no communicative skill below 50"
    out = et._pte(txt)
    assert out is not None
    assert out["overall"] == 58
    assert out["writing"] == 50


def test_toefl_table_layout():
    txt = "TOEFL iBT     60     12"
    out = et._toefl(txt)
    assert out is not None
    assert out["overall"] == 60
    assert out["reading"] == 12


def test_toefl_label_only_with_overall():
    txt = "TOEFL iBT     78"
    out = et._toefl(txt)
    assert out is not None
    assert out["overall"] == 78


def test_cambridge_table_layout():
    txt = "Cambridge English (CAE)     176"
    assert et._cambridge(txt) == 176.0


def test_duolingo_table_layout():
    txt = "Duolingo English Test     105"
    assert et._duolingo(txt) == 105.0


def test_duolingo_bare_det():
    txt = "DET     115"
    assert et._duolingo(txt) == 115.0


def test_pte_progression_prose_does_not_false_match():
    # Architect-flagged regression: "PTE 70 then PTE 80" used to be
    # parsed as overall=70, min=80. The negative-lookahead on `\bpte\b`
    # in the table fallback now forces the match to a single row, and
    # the min<=overall sanity gate is the second line of defence.
    txt = "Acceptable scores include PTE 70 then PTE 80 in some pathways."
    out = et._pte(txt)
    # The broad pattern 3 still claims an overall of 70 (no subscores)
    # — that's fine, it's a single number; what we MUST not see is the
    # fabricated overall+min pair.
    assert out is None or out.get("listening") is None


def test_pte_min_above_overall_rejected():
    # If the only two candidate numbers in the PTE band have min > overall,
    # they are NOT a valid row pairing and the table fallback rejects.
    txt = "PTE 50 ... 80"  # 80 > 50 → impossible row
    out = et._pte(txt)
    assert out is None or out.get("listening") is None


def test_full_pdf_table_extracts_all_five():
    """End-to-end: a single ASA-style requirements table must yield
    IELTS + PTE + TOEFL + CAE + Duolingo simultaneously, not just IELTS."""
    txt = (
        "International English Requirements\n"
        "Test                        Overall   Min skill\n"
        "IELTS Academic              6.0       5.5\n"
        "PTE Academic                50        36\n"
        "TOEFL iBT                   60        12\n"
        "Cambridge English (CAE)     169\n"
        "Duolingo English Test       95\n"
    )
    pte = et._pte(txt)
    toefl = et._toefl(txt)
    assert pte is not None and pte["overall"] == 50
    assert toefl is not None and toefl["overall"] == 60
    assert et._cambridge(txt) == 169.0
    assert et._duolingo(txt) == 95.0


# --- T207/T208 vision-output prod-bug regression ----------------------------


def test_ielts_bare_overall_format_from_gemini_vision():
    """Bug: prod ASA scrape showed IELTS=— for every staged course even
    when per_course_vision printed `IELTS overall: 6.0`. The other
    patterns require either `no band below X` or per-skill subscores; the
    bare-overall shape produced by Gemini Vision wasn't covered. Pattern
    4.5 fixes this — must extract 6.0 cleanly from the OCR format."""
    text = (
        "IELTS overall: 6.0\n"
        "PTE overall: 50\n"
        "TOEFL iBT: 60\n"
        "Cambridge Advanced: 169\n"
    )
    res = et._ielts(text)
    assert res is not None and res["overall"] == 6.0


def test_pte_bare_overall_format_from_gemini_vision():
    """Same bug for PTE: vision OCR text `PTE overall: 50` must parse."""
    text = (
        "IELTS overall: 6.0\n"
        "PTE overall: 50\n"
        "TOEFL iBT: 60\n"
    )
    res = et._pte(text)
    assert res is not None and res["overall"] == 50


def test_vision_format_full_dump_extracts_all_five():
    """End-to-end on the exact text Gemini Vision returns per the
    per_course_vision prompt — must extract IELTS, PTE, TOEFL, CAE,
    Duolingo (one of which was missing in prod)."""
    text = (
        "IELTS overall: 6.5\n"
        "IELTS listening: 6.0\n"
        "PTE overall: 58\n"
        "TOEFL iBT: 79\n"
        "Cambridge Advanced: 176\n"
        "Duolingo English Test: 105\n"
    )
    assert et._ielts(text) is not None
    assert et._ielts(text)["overall"] == 6.5
    assert et._pte(text) is not None
    assert et._pte(text)["overall"] == 58
    assert et._toefl(text) is not None
    assert et._toefl(text)["overall"] == 79
    assert et._cambridge(text) == 176.0
    assert et._duolingo(text) == 105.0


def test_rich_pattern_still_wins_over_bare_overall():
    """Regression guard: when both `IELTS overall 6.5 with no band below
    6.0` AND a separate bare overall appear, the rich Pattern 1 (with
    subscores) must win — Pattern 4.5 only fires as a fallback."""
    text = "IELTS Academic overall 6.5 with no individual band below 6.0."
    res = et._ielts(text)
    assert res is not None
    assert res["overall"] == 6.5
    # Pattern 1 fills all subscores; Pattern 4.5 leaves them None.
    assert res.get("listening") == 6.0


# ─────────────────────────────────────────────────────────────────────
# Bug L: Gemini Vision occasionally returns the verbose phrasing
# "IELTS Academic Overall Band Score: 6.5" / "PTE Academic Overall
# score: 58" instead of the terse "IELTS overall: 6.5" the prompt asks
# for. Captured below is the literal text Gemini returned for ASA's
# MaSTER.png (the English-requirements screenshot embedded on every
# IT Master page). Before the english_test fix the extractor only
# caught TOEFL via the table-layout pattern and silently dropped IELTS
# + PTE entirely, leaving IELTS=— on every staged ASA Master.
# ─────────────────────────────────────────────────────────────────────


_MASTER_PNG_GEMINI_OUTPUT = (
    "IELTS Academic Overall Band Score: 6.5\n"
    "IELTS Academic listening: 6\n"
    "IELTS Academic reading: 6\n"
    "IELTS Academic writing: 6\n"
    "IELTS Academic speaking: 6\n"
    "PTE Academic Overall score: 58\n"
    "PTE Academic minimum in each skill: 60\n"
    "TOEFL iBT Overall score: 85\n"
    "TOEFL iBT minimum in each skill: 20\n"
)


def test_ielts_verbose_overall_band_score_phrasing():
    """`IELTS Academic Overall Band Score: 6.5` plus per-skill `: 6` lines
    must yield overall=6.5 AND each sub-band=6.0."""
    res = et._ielts(_MASTER_PNG_GEMINI_OUTPUT)
    assert res is not None, "verbose IELTS phrasing must parse"
    assert res["overall"] == 6.5
    assert res["listening"] == 6.0
    assert res["reading"] == 6.0
    assert res["writing"] == 6.0
    assert res["speaking"] == 6.0


def test_pte_verbose_overall_score_phrasing():
    """`PTE Academic Overall score: 58` must yield overall=58. The
    nonsensical `minimum in each skill: 60` (60 > 58) must NOT be
    accepted as a sub-band requirement."""
    res = et._pte(_MASTER_PNG_GEMINI_OUTPUT)
    assert res is not None
    assert res["overall"] == 58
    # min > overall is a logical impossibility, so Pattern 2 must drop
    # the sub-band; Pattern 2.5 then fires and returns sub=None.
    assert res.get("listening") in (None, 0)


def test_toefl_verbose_extraction_unchanged():
    """The existing TOEFL table-layout pattern already handles this
    shape; pin it so a future english_test refactor doesn't regress it."""
    res = et._toefl(_MASTER_PNG_GEMINI_OUTPUT)
    assert res is not None
    assert res["overall"] == 85


# ─────────────────────────────────────────────────────────────────────
# Task 55 — "Pearson Test of English" full-name recognised as PTE in
# the equivalence-table fallback (_classify_test_label / _is_equivalence_table).
# ─────────────────────────────────────────────────────────────────────


def test_classify_test_label_pearson_full_name():
    """`_classify_test_label` must map 'pearson test of english' → 'pte'."""
    assert et._classify_test_label("pearson test of english") == "pte"


def test_classify_test_label_pearson_academic():
    """`_classify_test_label` must map 'pearson test of english academic' → 'pte'."""
    assert et._classify_test_label("pearson test of english academic") == "pte"


def test_is_equivalence_table_pearson_header(tmp_path):
    """A table whose header says 'Pearson Test of English' (no 'PTE' abbreviation)
    must still be recognised as an equivalence table."""
    from bs4 import BeautifulSoup

    html = """
    <table>
      <thead>
        <tr>
          <th>IELTS</th>
          <th>Pearson Test of English</th>
          <th>TOEFL iBT</th>
        </tr>
        <tr>
          <th>Overall</th>
          <th>Overall</th>
          <th>Overall</th>
        </tr>
      </thead>
      <tbody>
        <tr><td>6.5</td><td>58</td><td>79</td></tr>
      </tbody>
    </table>
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    assert et._is_equivalence_table(table) is True


def test_classify_test_label_pte_abbreviation_unchanged():
    """Regression guard: the existing 'pte' abbreviation still maps correctly."""
    assert et._classify_test_label("pte") == "pte"
    assert et._classify_test_label("pte academic") == "pte"


# ─────────────────────────────────────────────────────────────────────
# Task 56 — End-to-end: _parse_equivalence_table must yield the correct
# PTE score when the column header uses the full "Pearson Test of English"
# name instead of the "PTE" abbreviation.
# ─────────────────────────────────────────────────────────────────────


def test_parse_equivalence_table_pearson_full_name_extracts_pte_score():
    """_parse_equivalence_table must read the PTE score from a column whose
    header says 'Pearson Test of English' (not 'PTE').  This catches any
    row-parser regression that the label-classifier tests (Task 55) cannot
    detect on their own."""
    from bs4 import BeautifulSoup

    html = """
    <table>
      <thead>
        <tr>
          <th>IELTS</th>
          <th>Pearson Test of English</th>
          <th>TOEFL iBT</th>
        </tr>
        <tr>
          <th>Overall</th>
          <th>Overall</th>
          <th>Overall</th>
        </tr>
      </thead>
      <tbody>
        <tr><td>6.5</td><td>58</td><td>79</td></tr>
        <tr><td>6.0</td><td>50</td><td>60</td></tr>
      </tbody>
    </table>
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    mapping = et._parse_equivalence_table(table)

    assert mapping, "_parse_equivalence_table returned empty dict"
    assert 6.5 in mapping, "IELTS 6.5 row not found in mapping"
    assert mapping[6.5].get("pte") == 58.0, (
        f"Expected PTE 58 for IELTS 6.5, got {mapping[6.5]}"
    )
    assert 6.0 in mapping, "IELTS 6.0 row not found in mapping"
    assert mapping[6.0].get("pte") == 50.0, (
        f"Expected PTE 50 for IELTS 6.0, got {mapping[6.0]}"
    )
    assert mapping[6.5].get("toefl") == 79.0
    assert mapping[6.0].get("toefl") == 60.0
