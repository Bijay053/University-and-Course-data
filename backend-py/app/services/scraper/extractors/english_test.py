"""IELTS / PTE / TOEFL / Cambridge / Duolingo extractor.

Ported from Node ``extractEnglishRequirements`` family in
``artifacts/api-server/src/routes/scrape.ts`` (lines 2426-3175).
We keep the same multi-pattern cascade: each test runs three patterns
("with no band below X", "X overall with Y in each band", explicit
subscores) before falling through to a broad "<TEST> <number>" match.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from app.services.scraper.extractors._text import compact, html_to_text
from app.services.scraper.extractors.base import ExtractionResult

log = logging.getLogger(__name__)


field_keys = (
    "ielts_overall",
    "pte_overall",
    "toefl_overall",
    "cambridge_overall",
    "duolingo_overall",
)


# --- IELTS (overall + subscores 4.0-9.0) -------------------------------------
def _ielts(text: str) -> dict[str, float] | None:
    # Pattern 1: "IELTS overall 6.0 with no band below 5.5"
    # — also matches "Academic IELTS Overall 6.0, with no band below 5.5"
    #   (PDF policy phrasing; allow leading "Academic " prefix and a
    #    short punctuation bridge between the overall score and the
    #    "no band below" clause).
    # — also matches "IELTS Academic: Overall score 6.5, with no band below 6.0"
    #   (VIT prose phrasing; the optional `(?:score\s+|band\s+|of\s+)?`
    #    bridge between "overall" and the digit was missing in PR-1 and
    #    caused 100% of VIT staged rows to land with IELTS=— even though
    #    the prose plainly says it. Same fix below for PTE/TOEFL).
    m = re.search(
        r"(?:academic\s+)?ielts(?:\s+academic)?[^a-z0-9]{0,20}overall\s*"
        r"(?:score\s+|band\s+|score\s+of\s+|of\s+)?"
        r"([0-9]+(?:\.[0-9]+)?)"
        r"[^a-z0-9]{0,15}(?:with\s*)?(?:no\s+(?:individual\s+)?band\s+(?:below|less\s+than)|"
        r"minimum\s+of|no\s+score\s+less\s+than)\s*([0-9]+(?:\.[0-9]+)?)",
        text,
        re.I,
    )
    if m:
        ov, mn = float(m.group(1)), float(m.group(2))
        if 4 <= ov <= 9 and 4 <= mn <= 9:
            return {"overall": ov, "listening": mn, "reading": mn, "writing": mn, "speaking": mn}

    # Pattern 2: "IELTS 6.5 overall with 6.0 in each band"
    m = re.search(
        r"ielts(?:\s+academic)?[^a-z0-9]{0,20}([0-9]+(?:\.[0-9]+)?)\s*overall"
        r"[^a-z0-9]{0,20}(?:with\s*)?([0-9]+(?:\.[0-9]+)?)\s*"
        r"(?:in\s+each\s+(?:band|component|section)|each\s+(?:band|component|section))",
        text,
        re.I,
    )
    if m:
        ov, ea = float(m.group(1)), float(m.group(2))
        if 4 <= ov <= 9 and 4 <= ea <= 9:
            return {"overall": ov, "listening": ea, "reading": ea, "writing": ea, "speaking": ea}

    # Pattern 3: explicit subscores in order
    m = re.search(
        r"ielts(?:\s+academic)?.*?overall\s*([0-9]+(?:\.[0-9]+)?).*?"
        r"listening\s*([0-9]+(?:\.[0-9]+)?).*?reading\s*([0-9]+(?:\.[0-9]+)?).*?"
        r"writing\s*([0-9]+(?:\.[0-9]+)?).*?speaking\s*([0-9]+(?:\.[0-9]+)?)",
        text,
        re.I | re.S,
    )
    if m:
        return {
            "overall": float(m.group(1)),
            "listening": float(m.group(2)),
            "reading": float(m.group(3)),
            "writing": float(m.group(4)),
            "speaking": float(m.group(5)),
        }

    # Pattern 4: overall near "ielts" + standalone subscores.
    # The optional `(?:\s+(?:band\s+)?score)?` bridge after "overall"
    # lets us also catch Gemini Vision's verbose phrasing — e.g.
    # "IELTS Academic Overall Band Score: 6.5" — which is what ASA's
    # MaSTER.png OCR returns. The sub-score regexes use `[\s:.\-]+` (not
    # `\s*`) so "IELTS Academic listening: 6" parses; the leading `\b`
    # plus a 12-char window (with no other digit and no other test name)
    # keeps us from picking up unrelated numbers from elsewhere on the page.
    overall_m = re.search(
        r"ielts(?:\s+academic)?.{0,120}?overall(?:\s+band)?(?:\s+score)?"
        r"[\s:.\-]+([0-9]+(?:\.[0-9]+)?)",
        text,
        re.I | re.S,
    )
    listen_m = re.search(r"\blistening[\s:.\-]+([0-9]+(?:\.[0-9]+)?)\b", text, re.I)
    read_m = re.search(r"\breading[\s:.\-]+([0-9]+(?:\.[0-9]+)?)\b", text, re.I)
    write_m = re.search(r"\bwriting[\s:.\-]+([0-9]+(?:\.[0-9]+)?)\b", text, re.I)
    speak_m = re.search(r"\bspeaking[\s:.\-]+([0-9]+(?:\.[0-9]+)?)\b", text, re.I)
    if overall_m and (listen_m or read_m or write_m or speak_m):
        ov = float(overall_m.group(1))
        if 4 <= ov <= 9:
            def _sub(m: re.Match | None) -> float | None:
                if not m:
                    return None
                v = float(m.group(1))
                # IELTS sub-bands live in the same 4-9 range as overall;
                # anything outside is a false positive from neighbouring
                # text (e.g. "writing 60" caught from a PTE skill row).
                return v if 4 <= v <= 9 else None
            return {
                "overall": ov,
                "listening": _sub(listen_m),
                "reading": _sub(read_m),
                "writing": _sub(write_m),
                "speaking": _sub(speak_m),
            }

    # Pattern 4.5 (vision-friendly bare overall): "IELTS overall: 6.5"
    # — the format Gemini Vision returns per the per_course_vision prompt
    # template. Pattern 5 below uses `[^a-z0-9]{0,50}?` between "ielts"
    # and the digit, but the word "overall" between them blocks the
    # match (it's letters). Without this pattern, prod ASA scrape showed
    # IELTS=— for every staged course even when vision OCR printed
    # `IELTS overall: 6.0`. Placed AFTER patterns 1-4 so a richer match
    # (with "no band below" / per-skill subscores) wins, but BEFORE
    # Pattern 5 so this exact-shape match wins over ambiguous broad hits.
    # Optional "(band )?score" bridge mirrors Pattern 4 — handles Gemini's
    # "IELTS Academic Overall Band Score: 6.5" output that previously left
    # IELTS=— even when overall was clearly stated.
    m = re.search(
        r"\bielts(?:\s+academic)?\s+overall(?:\s+band)?(?:\s+score)?"
        r"[\s:.\-]{0,8}([4-9](?:\.[0-9])?)\b",
        text,
        re.I,
    )
    if m:
        ov = float(m.group(1))
        if 4 <= ov <= 9:
            return {"overall": ov, "listening": None, "reading": None, "writing": None, "speaking": None}

    # Pattern 5: broad "minimum IELTS 6.0", "IELTS 6.0 or higher", "IELTS: 6.5",
    # "IELTS (Academic): 6.5" — bridge uses [^\n0-9] (not [^a-z0-9]) so it
    # can cross lowercase words like "(Academic)" without stopping early.
    broad = re.search(
        r"(?:minimum\s+)?ielts[^\n0-9]{0,80}?([4-9](?:\.[05])?)",
        text,
        re.I,
    ) or re.search(
        r"ielts[^a-z0-9]{0,80}?([4-9](?:\.[05])?)\s*"
        r"(?:or\s+(?:above|higher|more)|minimum|overall|and\s+above|plus)",
        text,
        re.I,
    )
    if broad:
        ov = float(broad.group(1))
        if 4 <= ov <= 9:
            return {"overall": ov, "listening": None, "reading": None, "writing": None, "speaking": None}

    # Pattern 6: score BEFORE the IELTS keyword — "a score of 6.5 on the IELTS
    # test", "6.5 in IELTS Academic", "at least 7.0 on IELTS", "band score of
    # 6.5 on the IELTS Academic examination".
    #
    # All 5 earlier patterns start with the "ielts" keyword and look forward to
    # a number. This reverse-order gap means pages that phrase requirements as
    # "achieve 6.5 on the IELTS" or "a band score of 6.5 in IELTS Academic"
    # silently return None for every course even when the score is clearly stated.
    #
    # We require "in" or "on" between the digit and "ielts" to keep the match
    # tight — bare "6.5 ielts" (no preposition) is too ambiguous. The word
    # boundary `\b` after "ielts" prevents matching "ielts academic" when the
    # label continues with subscores on the same line.
    # The negative lookbehind `(?<![0-9.])` prevents matching the trailing
    # digit of a larger number: in "3.5 on the IELTS", `\b` alone would still
    # match "5" (because "." is a non-word char), giving the spurious score 5.0.
    # The lookbehind requires that the digit not be preceded by another digit
    # or decimal point, so "5" in "3.5" is correctly excluded.
    m = re.search(
        r"(?<![0-9.])([4-9](?:\.[05])?)\s+(?:in|on)\s+(?:the\s+)?(?:academic\s+)?ielts\b",
        text,
        re.I,
    )
    if m:
        ov = float(m.group(1))
        if 4 <= ov <= 9:
            return {"overall": ov, "listening": None, "reading": None, "writing": None, "speaking": None}

    return None


# --- PTE (10-90) -------------------------------------------------------------
def _pte(text: str) -> dict[str, float] | None:
    # Pattern 1 (rich): "PTE Academic 50 with no skill below 36"
    # — also matches "PTE Academic: Overall score 58, with no communicative
    #   skill below 50" (VIT-style prose where "score" sits between
    #   "overall" and the digit; same regression class as IELTS pattern 1).
    m = re.search(
        r"pte(?:\s+academic)?[^a-z0-9]{0,20}"
        r"(?:overall\s*(?:score\s+|band\s+|score\s+of\s+|of\s+)?)?"
        r"([0-9]+(?:\.[0-9]+)?)\s*"
        r"(?:with\s*)?(?:no\s+(?:communicative\s+)?skill\s+below|minimum\s+of|"
        r"no\s+score\s+less\s+than)\s*([0-9]+(?:\.[0-9]+)?)",
        text,
        re.I,
    )
    if m:
        ov, mn = float(m.group(1)), float(m.group(2))
        if 10 <= ov <= 90 and 10 <= mn <= 90:
            return {"overall": ov, "listening": mn, "reading": mn, "writing": mn, "speaking": mn}
    # Pattern 2 (table): "PTE Academic | 50 | 36"  /  "PTE  50  36"
    # PDF tables flatten to whitespace-separated runs after pypdf
    # extraction. Two adjacent numbers in the PTE band → overall + min.
    # Bug G: Without this, ASA's requirements PDF emits IELTS but
    # nothing else because the table layout breaks the rich pattern.
    #
    # Negative-lookahead `(?:(?!\bpte\b).)` between the two numbers
    # prevents the false match the architect flagged: prose like
    # "PTE 70 then PTE 80" used to be parsed as overall=70 / min=80,
    # fabricating a subscore from a comparison sentence. Requiring no
    # second `pte` mention between the captured numbers narrows the
    # match to a single row.
    m = re.search(
        r"pte(?:\s+academic)?(?:(?!\bpte\b)[^\n0-9]){1,60}?([1-9][0-9])\b"
        r"(?:(?!\bpte\b)[^\n0-9]){1,40}?([1-9][0-9])\b",
        text,
        re.I,
    )
    if m:
        ov, mn = float(m.group(1)), float(m.group(2))
        # The minimum-skill score must not exceed the overall — that's
        # the second sanity gate against accidentally pairing two
        # unrelated numbers from neighbouring rows.
        if 10 <= ov <= 90 and 10 <= mn <= ov:
            return {"overall": ov, "listening": mn, "reading": mn, "writing": mn, "speaking": mn}
    # Pattern 2.5 (vision-friendly bare overall): "PTE overall: 50" /
    # "PTE Academic overall 58" — Gemini Vision's own response format
    # per the per_course_vision prompt template. Pattern 3 below uses
    # `[^a-z0-9]{0,40}?` between "pte" and the digit, but the word
    # "overall" between them blocks the match (it's letters). Without
    # this pattern, prod ASA scrape showed PTE=— for every staged
    # course even when vision OCR printed `PTE overall: 50`. Placed
    # before Pattern 3 so the explicit "overall" shape wins.
    # Optional "(band )?score" bridge handles Gemini's verbose phrasing
    # — e.g. "PTE Academic Overall score: 58" from ASA's MaSTER.png
    # OCR — that previously left PTE=— even when the value was clearly
    # stated. Mirrors the same fix to IELTS Pattern 4.5.
    m = re.search(
        r"\bpte(?:\s+academic)?\s+overall(?:\s+band)?(?:\s+score)?"
        r"[\s:.\-]{0,8}([1-9][0-9])\b",
        text,
        re.I,
    )
    if m:
        ov = float(m.group(1))
        if 10 <= ov <= 90:
            return {"overall": ov, "listening": None, "reading": None, "writing": None, "speaking": None}

    # Pattern 3 (broad): "PTE 50" / "PTE: 50" / "PTE Academic: 58" /
    # "PTE (Academic): 58" — bridge uses [^\n0-9] so it can cross lowercase
    # words like "(Academic): " that [^a-z0-9] would stop at.
    m = re.search(
        r"pte[^\n0-9]{0,60}?([1-9][0-9])\b", text, re.I
    )
    if m:
        ov = float(m.group(1))
        if 10 <= ov <= 90:
            return {"overall": ov, "listening": None, "reading": None, "writing": None, "speaking": None}

    # Pattern 3b (full name): "Pearson Test of English Academic 58" /
    # "Pearson Test of English: 50" — some institutions write out the full
    # test name rather than the "PTE" abbreviation.  Placed after Pattern 3
    # to keep it as a catch-all for the long-form variant.
    m = re.search(
        r"pearson\s+test\s+of\s+english(?:\s+academic)?[^\n0-9]{0,60}?([1-9][0-9])\b",
        text,
        re.I,
    )
    if m:
        ov = float(m.group(1))
        if 10 <= ov <= 90:
            return {"overall": ov, "listening": None, "reading": None, "writing": None, "speaking": None}

    # Pattern 4 (reverse-order): "50 in PTE Academic" / "a score of 50 in the
    # PTE" / "50 on Pearson Test of English Academic".
    #
    # All three earlier patterns anchor on the PTE keyword and look forward to
    # a number.  This reverse-order gap means phrases like "achieve 50 in PTE
    # Academic" or "50 on the Pearson Test of English" silently return None.
    #
    # We require "in" or "on" between the digit and the test name to keep the
    # match tight — bare "50 PTE" (no preposition) is ambiguous in table
    # contexts.  The negative lookbehind (?<![0-9.]) prevents matching the
    # trailing digit of a larger number (e.g. "150" → "50").
    m = re.search(
        r"(?<![0-9.])([1-9][0-9])\s+(?:in|on)\s+(?:the\s+)?"
        r"(?:pearson\s+test\s+of\s+english|pte)(?:\s+academic)?\b",
        text,
        re.I,
    )
    if m:
        ov = float(m.group(1))
        if 10 <= ov <= 90:
            return {"overall": ov, "listening": None, "reading": None, "writing": None, "speaking": None}

    return None


# --- TOEFL (0-120) -----------------------------------------------------------
def _toefl(text: str) -> dict[str, float] | None:
    # Pattern 1 (rich): "TOEFL iBT 60 with no section below 12"
    # — also matches "TOEFL iBT: Overall score 87, with no section below 17"
    #   (VIT-style prose; same regression class as IELTS pattern 1).
    m = re.search(
        r"toefl(?:\s+ibt)?[^a-z0-9]{0,20}"
        r"(?:overall\s*(?:score\s+|band\s+|score\s+of\s+|of\s+)?)?"
        r"([0-9]+(?:\.[0-9]+)?)\s*"
        r"(?:with\s*)?(?:no\s+(?:band|section|subscore)\s+below|minimum\s+of|"
        r"no\s+score\s+less\s+than)\s*([0-9]+(?:\.[0-9]+)?)",
        text,
        re.I,
    )
    if m:
        ov, mn = float(m.group(1)), float(m.group(2))
        if 0 <= ov <= 120 and 0 <= mn <= 30:
            return {"overall": ov, "listening": mn, "reading": mn, "writing": mn, "speaking": mn}
    # Table layout: "TOEFL iBT  60  12" or "TOEFL | 60 | 12".
    m = re.search(
        r"toefl(?:\s+ibt)?[^\n0-9]{1,60}?([0-9]{2,3})\b[^\n0-9]{1,40}?([0-9]{1,2})\b",
        text,
        re.I,
    )
    if m:
        ov, mn = float(m.group(1)), float(m.group(2))
        if 0 <= ov <= 120 and 0 <= mn <= 30:
            return {"overall": ov, "listening": mn, "reading": mn, "writing": mn, "speaking": mn}
    m = re.search(r"toefl(?:\s+ibt)?[:\s]+([0-9]{2,3})", text, re.I)
    if m:
        ov = float(m.group(1))
        if 0 <= ov <= 120:
            return {"overall": ov, "listening": None, "reading": None, "writing": None, "speaking": None}
    # Pattern 2.5 (vision-friendly overall): "TOEFL overall: 87" /
    # "TOEFL iBT overall 87" / "TOEFL iBT Overall score: 87" — Gemini
    # Vision's own response format.  Placed before the broad lookahead
    # fallback so the explicit "overall" keyword shape wins and produces a
    # result even if the broad fallback would also fire.  Mirrors PTE
    # Pattern 2.5 and IELTS Pattern 4.5.
    m = re.search(
        r"\btoefl(?:\s+ibt)?\s+overall(?:\s+score)?[\s:.\-]{0,8}([0-9]{2,3})\b",
        text,
        re.I,
    )
    if m:
        ov = float(m.group(1))
        if 0 <= ov <= 120:
            return {"overall": ov, "listening": None, "reading": None, "writing": None, "speaking": None}
    # Loosest fallback: "TOEFL ... 60" within a short window — covers
    # PDFs that render "TOEFL iBT     60" with multiple spaces.
    m = re.search(r"toefl(?:\s+ibt)?[^\n0-9]{1,60}?([0-9]{2,3})\b", text, re.I)
    if m:
        ov = float(m.group(1))
        if 30 <= ov <= 120:
            return {"overall": ov, "listening": None, "reading": None, "writing": None, "speaking": None}

    # Pattern 4 (reverse-order): "87 in TOEFL iBT" / "87 on the TOEFL" /
    # "a score of 87 on the TOEFL iBT".
    #
    # All earlier patterns anchor on the TOEFL keyword and look forward to a
    # number.  This gap means "achieve 87 in TOEFL iBT" silently returns None.
    # We require "in" or "on" to keep the match tight.  The negative lookbehind
    # (?<![0-9.]) prevents matching the trailing digits of larger numbers.
    # Floor of 30 avoids matching TOEFL section scores (0-30) as overall.
    m = re.search(
        r"(?<![0-9.])([0-9]{2,3})\s+(?:in|on)\s+(?:the\s+)?toefl(?:\s+ibt)?\b",
        text,
        re.I,
    )
    if m:
        ov = float(m.group(1))
        if 30 <= ov <= 120:
            return {"overall": ov, "listening": None, "reading": None, "writing": None, "speaking": None}

    return None


# --- Cambridge CAE / C1 Advanced / C2 Proficiency (140-230) -----------------
# Aliases recognised:
#   • "cambridge" / "cambridge english" (broad)
#   • "CAE" (Certificate in Advanced English — C1 level)
#   • "C1 Advanced" (renamed label post-2015)
#   • "CPE" (Certificate of Proficiency in English — C2 level)
#   • "C2 Proficiency" (renamed label post-2015)
# All variants use the same 140-230 numeric scale.
_CAM_ALT = r"(?:cambridge(?:\s+english)?|cae|cpe|c1\s*advanced|c2\s*proficiency)"


def _cambridge(text: str) -> float | None:
    for pat in (
        # Forward-order: keyword then 3-digit score within 40 chars.
        _CAM_ALT + r"[^0-9]{0,40}?(\d{3})",
        # Reverse-order: 3-digit score then keyword within 20 chars.
        r"(\d{3})[^0-9]{0,20}" + _CAM_ALT,
        # Table layout — wider window between label and number to clear
        # the cells in between. Capped at 80 chars so we don't cross row
        # boundaries.
        _CAM_ALT + r"[^\n0-9]{1,80}?(\d{3})",
    ):
        m = re.search(pat, text, re.I)
        if m:
            v = int(m.group(1))
            if 140 <= v <= 230:
                return float(v)
    return None


# --- Duolingo (50-160) -------------------------------------------------------
def _duolingo(text: str) -> float | None:
    for pat in (
        r"duolingo(?:\s+english\s+test)?[:\s]*(?:overall\s*(?:score\s*)?(?:of\s*)?)?(\d{2,3})",
        r"\bDET\b[:\s]+(\d{2,3})",
        # Table layout: "Duolingo English Test  105"
        r"duolingo(?:\s+english\s+test)?[^\n0-9]{1,80}?(\d{2,3})",
        # Bare DET in a table row.
        r"\bDET\b[^\n0-9]{1,40}?(\d{2,3})",
        # Reverse-order: "105 in the Duolingo English Test" / "105 on Duolingo"
        # / "105 in DET". All forward patterns anchor on the test name and look
        # forward to a score; this catches the inverted phrasing used by some
        # institutions. We require "in" or "on" + a boundary to keep the match
        # tight. Negative lookbehind prevents matching trailing digits of larger
        # numbers (e.g. "1105" → "105").
        r"(?<![0-9])(\d{2,3})\s+(?:in|on)\s+(?:the\s+)?(?:duolingo(?:\s+english\s+test)?|det)\b",
    ):
        m = re.search(pat, text, re.I)
        if m:
            v = int(m.group(1))
            if 50 <= v <= 160:
                return float(v)
    return None


def _emit(test: str, scores: dict[str, float] | float | None, snippet: str) -> list[ExtractionResult]:
    if scores is None:
        return []
    out: list[ExtractionResult] = []
    if isinstance(scores, dict):
        normalized: dict[str, Any] = {}
        for k, v in scores.items():
            if v is None:
                continue
            if k == "overall":
                normalized[f"{test}_overall"] = v
            else:
                normalized[f"{test}_{k}"] = v
        out.append(
            ExtractionResult(
                field_key=f"{test}_overall",
                value=scores.get("overall"),
                normalized=normalized,
                confidence=0.85,
                snippet=snippet[:200],
                method="regex",
            )
        )
    else:
        out.append(
            ExtractionResult(
                field_key=f"{test}_overall",
                value=scores,
                normalized={f"{test}_overall": scores},
                confidence=0.7,
                snippet=snippet[:200],
                method="regex",
            )
        )
    return out


# --- Equivalence-table fallback (PR-1.5 hot-fix #3) --------------------------
# Many AU/UK universities (VIT, Macquarie, etc.) only state IELTS in plain
# prose and bury PTE/TOEFL/CAE inside a multi-row equivalence <table>:
#
#   | IELTS | PTE  | TOEFL | CAE |
#   | 6.5   | 55   | 81    | 176 |
#
# The regex extractors below cannot read this layout — they search prose, not
# table cells, so the table values look like noise to them. Vision OCR on the
# table image is unreliable (in prod we saw 20 vision hits but only 11 PTE
# rows landed because vision often grabs the wrong cell). Parsing the HTML
# directly is both cheaper and accurate.
#
# Strategy: after the prose extractors run, if we have an IELTS overall but
# are missing PTE/TOEFL/CAE, look up the matching IELTS row in the page's
# equivalence table and fill from there. Only fills missing slots — never
# overwrites a higher-confidence prose extraction.

# Order matters — TOEFL/PTE/Cambridge headers commonly reference "as per
# IELTS website" as flavour text, so IELTS must be the last thing we check.
def _classify_test_label(label_lc: str) -> str | None:
    if "toefl" in label_lc:
        return "toefl"
    if "pte" in label_lc or "pearson" in label_lc:
        return "pte"
    if "cambridge" in label_lc or re.search(r"\bcae\b", label_lc):
        return "cambridge"
    if "duolingo" in label_lc or re.search(r"\bdet\b", label_lc):
        return "duolingo"
    if "kite" in label_lc:
        # KITE (Kaplan International Tools for English) is not a slot we score.
        return None
    if "ielts" in label_lc:
        return "ielts"
    return None


def _is_equivalence_table(table) -> bool:
    """A test-equivalence table mentions IELTS plus at least one other test."""
    headers = " ".join(th.get_text(" ", strip=True) for th in table.find_all("th"))
    headers_lc = headers.lower()
    if "ielts" not in headers_lc:
        return False
    return any(t in headers_lc for t in ("pte", "pearson", "toefl", "cambridge", "cae", "duolingo"))


def _parse_equivalence_table(table) -> dict[float, dict[str, float]]:
    """Return ``{ielts_overall: {pte: x, toefl: y, cambridge: z, ...}, ...}``.

    Handles two-row headers (group + sub-column) and rowspan/colspan cells.
    Returns an empty dict on any parse failure — never raises.
    """
    try:
        thead = table.find("thead")
        header_rows = thead.find_all("tr") if thead else []
        if len(header_rows) < 2:
            # Single-header tables (rare for equivalence layouts) — skip.
            return {}

        # Expand row-1 colspans → per-column test-group name.
        group_per_col: list[str | None] = []
        for th in header_rows[0].find_all(["th", "td"]):
            label = th.get_text(" ", strip=True).lower()
            span = int(th.get("colspan", "1") or "1")
            group_per_col.extend([_classify_test_label(label)] * span)

        # Expand row-2 colspans → per-column sub-label ("overall", "Listening", ...).
        sub_per_col: list[str] = []
        for th in header_rows[1].find_all(["th", "td"]):
            label = th.get_text(" ", strip=True).lower()
            span = int(th.get("colspan", "1") or "1")
            sub_per_col.extend([label] * span)

        n = max(len(group_per_col), len(sub_per_col))
        group_per_col += [None] * (n - len(group_per_col))
        sub_per_col += [""] * (n - len(sub_per_col))

        # Map each test → column index of its 'overall'.
        overall_col: dict[str, int] = {}
        # Map (test, sub_label) → column index for non-overall sub-skills.
        subskill_col: dict[tuple[str, str], int] = {}
        for i, (g, s) in enumerate(zip(group_per_col, sub_per_col)):
            if not g:
                continue
            if s == "overall":
                if g not in overall_col:
                    overall_col[g] = i
            elif s in ("listening", "writing", "reading", "speaking"):
                subskill_col[(g, s)] = i
        # IELTS sometimes labels its first column with prose like "band score"
        # rather than the literal "overall" — fall back to the leftmost
        # IELTS-group column when that happens.
        if "ielts" not in overall_col:
            for i, g in enumerate(group_per_col):
                if g == "ielts":
                    overall_col["ielts"] = i
                    break
        if "ielts" not in overall_col:
            return {}

        tbody = table.find("tbody")
        if not tbody:
            return {}

        out: dict[float, dict[str, float]] = {}
        # rowspan carry-over: column index → (text, remaining_rows).
        carry: dict[int, list] = {}
        for tr in tbody.find_all("tr"):
            cells: dict[int, str] = {}
            # Apply current carry first.
            for col, entry in list(carry.items()):
                cells[col] = entry[0]
                entry[1] -= 1
                if entry[1] <= 0:
                    del carry[col]
            # Now walk this row's <td>s, skipping columns already filled.
            col = 0
            for td in tr.find_all("td"):
                while col in cells and col < n:
                    col += 1
                if col >= n:
                    break
                text = td.get_text(" ", strip=True)
                colspan = int(td.get("colspan", "1") or "1")
                rowspan = int(td.get("rowspan", "1") or "1")
                for k in range(colspan):
                    cells[col + k] = text
                    if rowspan > 1:
                        carry[col + k] = [text, rowspan - 1]
                col += colspan

            ielts_text = cells.get(overall_col["ielts"], "").strip()
            mm = re.match(r"^([0-9]+(?:\.[0-9]+)?)$", ielts_text)
            if not mm:
                continue
            ielts_val = float(mm.group(1))
            if not (4 <= ielts_val <= 9):
                continue

            row_data: dict[str, float] = {}
            for test, c in overall_col.items():
                if test == "ielts":
                    continue
                t = cells.get(c, "").strip()
                m2 = re.match(r"^([0-9]+(?:\.[0-9]+)?)$", t)
                if not m2:
                    continue
                v = float(m2.group(1))
                # Apply per-test sanity bounds.
                if test == "pte" and not (10 <= v <= 90):
                    continue
                if test == "toefl" and not (0 <= v <= 120):
                    continue
                if test == "cambridge" and not (140 <= v <= 230):
                    continue
                if test == "duolingo" and not (50 <= v <= 160):
                    continue
                row_data[test] = v

            # Also capture sub-skill scores (e.g. pte_listening, pte_writing).
            for (test, sub), c in subskill_col.items():
                t = cells.get(c, "").strip()
                m3 = re.match(r"^([0-9]+(?:\.[0-9]+)?)$", t)
                if not m3:
                    continue
                v = float(m3.group(1))
                if test == "pte" and not (10 <= v <= 90):
                    continue
                if test == "toefl" and not (0 <= v <= 120):
                    continue
                row_data[f"{test}_{sub}"] = v

            if row_data:
                out[ielts_val] = row_data
        return out
    except Exception as exc:  # noqa: BLE001 — never break extraction here
        log.debug("_parse_equivalence_table failed: %s", exc)
        return {}


# Program-level column priority: higher number = prefer this column.
# "Bachelor and all Postgraduate programs" → priority 5 (wins over Diploma etc.)
_PROG_LEVEL_PRIORITY: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"\bpostgraduate\b|\bmaster\b|\bmba\b", re.I), 5),
    (re.compile(r"\bbachelor\b", re.I), 4),
    (re.compile(r"\bdiploma\b", re.I), 3),
    (re.compile(r"\bassociate\b|\beap\s*2\b|\bfoundation\s*2\b", re.I), 2),
    (re.compile(r"\bfoundation\b|\beap\b|\benglish\s+for\s+academic\b", re.I), 1),
]
_TEST_ROW_LABELS = re.compile(r"\b(ielts|pte|toefl|cambridge|cae|duolingo|det)\b", re.I)


def _parse_program_level_table(soup: BeautifulSoup) -> dict[str, float]:
    """Parse tables where COLUMN HEADERS are program levels and ROW LABELS are
    test names (e.g. KBS English Entry Requirements page):

        |                  | EAP 1 | Diploma | Bachelor & Postgrad |
        | IELTS            | 5.0   | 5.5     | 6.0                 |
        | PTE Academic     | 41    | 46      | 50                  |

    Returns scores for the HIGHEST-priority program level column found.
    Overrides prose-based extraction on multi-level pages (fixes picking 5.0
    instead of 6.0 when Foundation rows appear before Bachelor rows).
    """
    for table in soup.find_all("table"):
        try:
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            # ── Identify column headers (first row) ──────────────────────────
            header_cells = rows[0].find_all(["th", "td"])
            if len(header_cells) < 3:
                continue
            col_texts = [c.get_text(" ", strip=True) for c in header_cells]

            # ── Require at least one data row whose first cell is a test name ─
            first_data_cells = rows[1].find_all(["td", "th"])
            if not first_data_cells:
                continue
            if not _TEST_ROW_LABELS.search(first_data_cells[0].get_text(" ", strip=True)):
                continue

            # ── Score each column by program-level priority ───────────────────
            best_col = -1
            best_priority = 0
            for ci, col_text in enumerate(col_texts[1:], 1):
                for pattern, priority in _PROG_LEVEL_PRIORITY:
                    if pattern.search(col_text):
                        if priority > best_priority:
                            best_priority = priority
                            best_col = ci
                        break

            if best_col < 0 or best_priority < 3:
                continue

            # ── Extract test scores from the winning column ───────────────────
            result: dict[str, float] = {}
            for row in rows[1:]:
                cells = row.find_all(["td", "th"])
                if len(cells) <= best_col:
                    continue
                label = cells[0].get_text(" ", strip=True).lower()
                val = cells[best_col].get_text(" ", strip=True)

                if "ielts" in label:
                    m = re.search(r"([4-9](?:\.[05])?)", val)
                    if m:
                        ov = float(m.group(1))
                        if 4 <= ov <= 9:
                            result["ielts_overall"] = ov
                            # Also parse subscores: "6.0 for Speaking and Writing and 5.5 for Listening and Reading"
                            # Each "SCORE for SKILL [and SKILL]" group — stop before next number.
                            for sc_str, skills_str in re.findall(
                                r"([0-9]+(?:\.[0-9]+)?)\s+for\s+"
                                r"((?:(?:speaking|listening|reading|writing)"
                                r"(?:\s+and\s+(?![0-9])(?:speaking|listening|reading|writing))*)+)",
                                val, re.I,
                            ):
                                sc = float(sc_str)
                                if 4 <= sc <= 9:
                                    for skill_m in re.finditer(
                                        r"speaking|listening|reading|writing", skills_str, re.I
                                    ):
                                        result[f"ielts_{skill_m.group().lower()}"] = sc
                elif "pte" in label:
                    m = re.search(r"(?:score\s+of\s+)?([1-9][0-9])\b", val)
                    if m:
                        ov = float(m.group(1))
                        if 10 <= ov <= 90:
                            result["pte_overall"] = ov
                elif "toefl" in label:
                    m = re.search(r"(?:band\s+score\s+of\s+|total\s+)?(\d{2,3})\b", val)
                    if m:
                        ov = float(m.group(1))
                        if 0 <= ov <= 120:
                            result["toefl_overall"] = ov
                elif "cambridge" in label or re.search(r"\bcae\b", label):
                    m = re.search(r"(\d{3})\b", val)
                    if m:
                        ov = float(m.group(1))
                        if 140 <= ov <= 230:
                            result["cambridge_overall"] = ov

            if len(result) >= 2:
                return result
        except Exception as exc:  # noqa: BLE001
            log.debug("_parse_program_level_table row error: %s", exc)
    return {}


def _equivalence_fallback(
    html: str, results: list[ExtractionResult]
) -> list[ExtractionResult]:
    """Fill missing PTE/TOEFL/CAE/Duolingo from the page's equivalence table.

    Only fires when (a) the prose extractors found an IELTS overall,
    (b) at least one of PTE/TOEFL/CAE/Duolingo is still missing,
    (c) the page contains a parseable equivalence table whose IELTS row
    matches the extracted IELTS overall.
    """
    found = {r.field_key for r in results}
    if "ielts_overall" not in found:
        return []
    needed = {"pte_overall", "toefl_overall", "cambridge_overall", "duolingo_overall"}
    missing = needed - found
    if not missing:
        return []

    # The IELTS overall we already extracted from prose (top result wins).
    ielts_overall = next(
        (r.normalized.get("ielts_overall") for r in results
         if r.field_key == "ielts_overall" and r.normalized),
        None,
    )
    if ielts_overall is None:
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:  # noqa: BLE001
        log.debug("_equivalence_fallback: BS4 parse failed: %s", exc)
        return []

    extra: list[ExtractionResult] = []
    for table in soup.find_all("table"):
        if not _is_equivalence_table(table):
            continue
        mapping = _parse_equivalence_table(table)
        # Allow a small float tolerance (e.g. extracted 6.5 matches table 6.5).
        match_key = next(
            (k for k in mapping if abs(k - float(ielts_overall)) < 0.05),
            None,
        )
        if match_key is None:
            continue
        row = mapping[match_key]
        snippet = f"equivalence table row IELTS={match_key}"
        for test, val in row.items():
            field_key = f"{test}_overall"
            if field_key not in missing:
                continue
            extra.append(
                ExtractionResult(
                    field_key=field_key,
                    value=val,
                    normalized={field_key: val},
                    confidence=0.8,  # high — direct cell read, no OCR
                    snippet=snippet,
                    method="equivalence_table",
                )
            )
            missing.discard(field_key)
        if not missing:
            break
    return extra


async def extract(html: str, url: str) -> list[ExtractionResult]:
    text = compact(html_to_text(html))
    if not text:
        return []
    snippet = text[:500]
    results: list[ExtractionResult] = [
        *_emit("ielts", _ielts(text), snippet),
        *_emit("pte", _pte(text), snippet),
        *_emit("toefl", _toefl(text), snippet),
        *_emit("cambridge", _cambridge(text), snippet),
        *_emit("duolingo", _duolingo(text), snippet),
    ]
    # ── Program-level table: override prose results when the page has a table
    # with program-level column headers (Foundation | Diploma | Bachelor |
    # Postgraduate).  Prose extraction on such pages picks the first/lowest
    # row value (e.g. IELTS 5.0 for Foundation) instead of the target level
    # (6.0 for Bachelor+Postgraduate).  The table parser selects the
    # highest-priority column and overrides any already-extracted values.
    try:
        soup = BeautifulSoup(html, "html.parser")
        level_data = _parse_program_level_table(soup)
        if level_data:
            # Group level_data by test prefix (ielts, pte, toefl, cambridge).
            # Each test gets ONE ExtractionResult with all its band scores in
            # normalized — matching the convention established by _emit().
            test_groups: dict[str, dict[str, float]] = {}
            for field_key, val in level_data.items():
                prefix = field_key.split("_")[0]  # "ielts", "pte", …
                test_groups.setdefault(prefix, {})[field_key] = val

            # Drop existing results for any field now covered by the table.
            dominated = set(level_data.keys())
            results = [r for r in results if r.field_key not in dominated]

            for prefix, fields in test_groups.items():
                overall_key = f"{prefix}_overall"
                results.append(
                    ExtractionResult(
                        field_key=overall_key,
                        value=fields.get(overall_key),
                        normalized=fields,        # includes subscores
                        confidence=0.9,
                        snippet=f"program-level table → {fields}",
                        method="program_level_table",
                    )
                )
            log.debug(
                "english_test: program_level_table overrode %s → %s",
                dominated, level_data,
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("program_level_table failed: %s", exc)

    # Last-resort: equivalence-table lookup for tests not captured by prose.
    results.extend(_equivalence_fallback(html, results))
    return results
