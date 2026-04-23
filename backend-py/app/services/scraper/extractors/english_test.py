"""IELTS / PTE / TOEFL / Cambridge / Duolingo extractor.

Ported from Node ``extractEnglishRequirements`` family in
``artifacts/api-server/src/routes/scrape.ts`` (lines 2426-3175).
We keep the same multi-pattern cascade: each test runs three patterns
("with no band below X", "X overall with Y in each band", explicit
subscores) before falling through to a broad "<TEST> <number>" match.
"""
from __future__ import annotations

import re
from typing import Any

from app.services.scraper.extractors._text import compact, html_to_text
from app.services.scraper.extractors.base import ExtractionResult


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
    m = re.search(
        r"ielts(?:\s+academic)?[^a-z0-9]{0,20}overall\s*([0-9]+(?:\.[0-9]+)?)\s*"
        r"(?:with\s*)?(?:no\s+(?:individual\s+)?band\s+below|minimum\s+of|"
        r"no\s+score\s+less\s+than)\s*([0-9]+(?:\.[0-9]+)?)",
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

    # Pattern 4: overall near "ielts" + standalone subscores
    overall_m = re.search(
        r"ielts(?:\s+academic)?.{0,120}?overall\s*([0-9]+(?:\.[0-9]+)?)", text, re.I | re.S
    )
    listen_m = re.search(r"listening\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
    read_m = re.search(r"reading\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
    write_m = re.search(r"writing\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
    speak_m = re.search(r"speaking\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
    if overall_m and (listen_m or read_m or write_m or speak_m):
        ov = float(overall_m.group(1))
        if 4 <= ov <= 9:
            return {
                "overall": ov,
                "listening": float(listen_m.group(1)) if listen_m else None,
                "reading": float(read_m.group(1)) if read_m else None,
                "writing": float(write_m.group(1)) if write_m else None,
                "speaking": float(speak_m.group(1)) if speak_m else None,
            }

    # Pattern 5: broad "minimum IELTS 6.0", "IELTS 6.0 or higher", "IELTS: 6.5"
    broad = re.search(
        r"(?:minimum\s+)?ielts(?:\s+academic)?[^a-z0-9]{0,50}?([4-9](?:\.[05])?)",
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
    return None


# --- PTE (10-90) -------------------------------------------------------------
def _pte(text: str) -> dict[str, float] | None:
    m = re.search(
        r"pte(?:\s+academic)?[^a-z0-9]{0,20}(?:overall\s*)?([0-9]+(?:\.[0-9]+)?)\s*"
        r"(?:with\s*)?(?:no\s+(?:communicative\s+)?skill\s+below|minimum\s+of|"
        r"no\s+score\s+less\s+than)\s*([0-9]+(?:\.[0-9]+)?)",
        text,
        re.I,
    )
    if m:
        ov, mn = float(m.group(1)), float(m.group(2))
        if 10 <= ov <= 90 and 10 <= mn <= 90:
            return {"overall": ov, "listening": mn, "reading": mn, "writing": mn, "speaking": mn}
    m = re.search(
        r"pte(?:\s+academic)?[^a-z0-9]{0,40}?([1-9][0-9])\b", text, re.I
    )
    if m:
        ov = float(m.group(1))
        if 10 <= ov <= 90:
            return {"overall": ov, "listening": None, "reading": None, "writing": None, "speaking": None}
    return None


# --- TOEFL (0-120) -----------------------------------------------------------
def _toefl(text: str) -> dict[str, float] | None:
    m = re.search(
        r"toefl(?:\s+ibt)?[^a-z0-9]{0,20}(?:overall\s*)?([0-9]+(?:\.[0-9]+)?)\s*"
        r"(?:with\s*)?(?:no\s+(?:band|section|subscore)\s+below|minimum\s+of|"
        r"no\s+score\s+less\s+than)\s*([0-9]+(?:\.[0-9]+)?)",
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
    return None


# --- Cambridge CAE / C1 Advanced (140-230) -----------------------------------
def _cambridge(text: str) -> float | None:
    for pat in (
        r"(?:cambridge|cae|c1\s*advanced)[^0-9]{0,40}?(\d{3})",
        r"(\d{3})[^0-9]{0,20}(?:cambridge|cae|c1\s*advanced)",
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


async def extract(html: str, url: str) -> list[ExtractionResult]:
    text = compact(html_to_text(html))
    if not text:
        return []
    snippet = text[:500]
    return [
        *_emit("ielts", _ielts(text), snippet),
        *_emit("pte", _pte(text), snippet),
        *_emit("toefl", _toefl(text), snippet),
        *_emit("cambridge", _cambridge(text), snippet),
        *_emit("duolingo", _duolingo(text), snippet),
    ]
