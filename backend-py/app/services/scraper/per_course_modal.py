"""Per-course Bootstrap-modal English-test extractor (T002).

Mirrors Node per-course modal scan from
``artifacts/api-server/src/routes/scrape.ts`` (lines 8754-8917).

Many universities (notably VIT) embed a hidden ``.modal`` /
``[role=dialog]`` element directly on each course page that contains a
small concordance table with course-specific IELTS / PTE / TOEFL / CAE
values. The scores differ across degree levels (Cert/Diploma 5.5,
Bachelor 6.0, Master/MBA 6.5), so we MUST scan the modal of *each*
course page rather than caching a single modal across the catalogue.

Public entry-point: :func:`extract_modal_english`.

The extractor returns a flat ``dict`` of normalized fields:

* ``ielts_overall`` (float)
* ``pte_overall`` (int)
* ``toefl_overall`` (int)
* ``cambridge_overall`` (int)
* ``ielts_listening`` / ``ielts_reading`` / ``ielts_writing`` /
  ``ielts_speaking`` — each a float, set when sub-band info is recovered.

Plus an internal-only key ``__modal_summary`` (str) with the human-
readable line the orchestrator emits to the live log.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


# CSS selector for "looks like a modal" containers. Same union the Node
# scanner uses; intentionally broad to catch sites using lightbox /
# popup / dialog / role=dialog wrappers.
_MODAL_SELECTORS: tuple[str, ...] = (
    ".modal",
    "[class*=modal]",
    "[class*=popup]",
    "[role=dialog]",
    "[class*=lightbox]",
    "[class*=dialog]",
)

# Modal must clearly be an English-requirements panel: needs IELTS, PTE,
# TOEFL keywords, at least one digit, and a sensible length window so we
# don't latch on to a course-comparison modal that incidentally mentions
# the same words.
_MODAL_KEYWORD_RE = re.compile(r"IELTS", re.I)
_MODAL_PTE_RE = re.compile(r"PTE", re.I)
_MODAL_TOEFL_RE = re.compile(r"TOEFL", re.I)
_HAS_DIGIT_RE = re.compile(r"\d")
_MIN_MODAL_TEXT = 80
_MAX_MODAL_TEXT = 8000

# Numeric cell — strict integer or decimal.
_NUMERIC_CELL_RE = re.compile(r"^\d+(?:\.\d+)?$")

# IELTS sub-band patterns (mirrors Node lines 8854-8902).
_PATTERN_A = re.compile(
    r"IELTS\s+(?:Academic\s+)?(?:Overall\s+)?(\d+\.?\d*)\s*,?\s*"
    r"with\s+no\s+(?:individual\s+)?band(?:\s+score)?\s+below\s+(\d+\.?\d*)",
    re.I,
)
_PATTERN_A2 = (
    re.compile(r"no individual band (?:below|under|less than|score below) (\d+(?:\.\d+)?)", re.I),
    re.compile(r"no band(?:\s+score)? below (\d+(?:\.\d+)?)", re.I),
    re.compile(
        r"each (?:band|component|skill|section)(?:\s+score)?\s+"
        r"(?:of|at least|is|above)?\s*(\d+(?:\.\d+)?)",
        re.I,
    ),
    re.compile(r"minimum.*?band.*?(\d+(?:\.\d+)?)", re.I),
)
_PATTERN_B = re.compile(
    # Note: ``[^]`` is a JS regex idiom for "any char including newlines"
    # that Python's ``re`` doesn't support. Use ``[\s\S]`` instead.
    r"Listening[:\s]+(\d+(?:\.\d+)?)[\s\S]{0,30}"
    r"Reading[:\s]+(\d+(?:\.\d+)?)[\s\S]{0,30}"
    r"Writing[:\s]+(\d+(?:\.\d+)?)[\s\S]{0,30}"
    r"Speaking[:\s]+(\d+(?:\.\d+)?)",
    re.I,
)
_PATTERN_C = re.compile(
    r"\bL\s*(\d+\.?\d*)\s+R\s*(\d+\.?\d*)\s+W\s*(\d+\.?\d*)\s+S\s*(\d+\.?\d*)",
    re.I,
)


def _select_target_ielts(course_name: str, degree_level: str) -> float:
    """Return the IELTS score we EXPECT for this course based on its
    degree level — used to pick the right row in modals that contain
    multiple bands (Diploma 5.5, Bachelor 6.0, Postgrad 6.5).

    Mirrors Node lines 8779-8788.
    """
    cn = (course_name or "").lower()
    dl = (degree_level or "").lower()
    if (
        re.search(
            r"master|\bmba\b|postgrad|graduate (diploma|cert)|"
            r"\bgdba\b|\bgcba\b|\bgdits\b|\bgcits\b|"
            r"\bm\.?(sc|a|eng|ed|com|phil)\b",
            cn,
        )
        or re.search(r"master|postgrad|graduate", dl)
    ):
        return 6.5
    if re.search(r"diploma|certificate|\bcert\b|advanced diploma", cn) or re.search(
        r"diploma|certificate", dl
    ):
        return 5.5
    if re.search(r"bachelor|\bbbus\b|\bbits\b|\bb\.?(eng|sc|com|a|ed)\b", cn) or re.search(
        r"bachelor", dl
    ):
        return 6.0
    return 6.0  # safe default


def _classify_row_numbers(numbers: list[float]) -> dict[str, float | int]:
    """Bucket a row's numeric cells into IELTS / PTE / TOEFL / CAE slots
    using the same value-range heuristic Node uses (lines 8804-8810).

    * IELTS: any number in [4, 9]
    * PTE: integer in [10, 90]
    * CAE: integer in [140, 230]
    * TOEFL: integer in [30, 120], excluding the cell already claimed by PTE.
    """
    result: dict[str, float | int] = {}
    ielts = next((v for v in numbers if 4 <= v <= 9), None)
    pte = next(
        (int(v) for v in numbers if v.is_integer() and 10 <= v <= 90),
        None,
    )
    cae = next(
        (int(v) for v in numbers if v.is_integer() and 140 <= v <= 230),
        None,
    )
    toefl = next(
        (
            int(v)
            for v in numbers
            if v.is_integer() and 30 <= v <= 120 and (pte is None or int(v) != pte)
        ),
        None,
    )
    if ielts is not None:
        result["ielts_overall"] = float(ielts)
    if pte is not None:
        result["pte_overall"] = int(pte)
    if toefl is not None:
        result["toefl_overall"] = int(toefl)
    if cae is not None:
        result["cambridge_overall"] = int(cae)
    return result


def _find_modal_html(html: str) -> str | None:
    """Locate the first English-requirement modal in ``html`` and return
    its outer-HTML (capped at 25 KB), or ``None`` if no modal qualifies.
    """
    soup = BeautifulSoup(html, "html.parser")
    selector = ", ".join(_MODAL_SELECTORS)
    for el in soup.select(selector):
        text = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if not text:
            continue
        if not (_MODAL_KEYWORD_RE.search(text) and _MODAL_PTE_RE.search(text)
                and _MODAL_TOEFL_RE.search(text) and _HAS_DIGIT_RE.search(text)):
            continue
        if not (_MIN_MODAL_TEXT <= len(text) <= _MAX_MODAL_TEXT):
            continue
        outer = str(el)
        if outer and len(outer) < 25_000:
            return outer
    return None


def _extract_sub_bands(scan_text: str) -> dict[str, float]:
    """Extract IELTS sub-band values (L/R/W/S) from ``scan_text`` using
    patterns A, A2, B, C from the Node implementation."""
    out: dict[str, float] = {}

    # Pattern A — most specific (VIT canonical).
    m = _PATTERN_A.search(scan_text)
    if m:
        try:
            min_band = float(m.group(2))
        except ValueError:
            min_band = 0.0
        if 4 <= min_band <= 9:
            for slot in ("ielts_listening", "ielts_reading", "ielts_writing", "ielts_speaking"):
                out[slot] = min_band

    # Pattern A2 — looser variants of "no individual band below X".
    if "ielts_listening" not in out:
        for pat in _PATTERN_A2:
            m2 = pat.search(scan_text)
            if m2:
                try:
                    mb = float(m2.group(1))
                except ValueError:
                    continue
                if 4 <= mb <= 9:
                    for slot in (
                        "ielts_listening",
                        "ielts_reading",
                        "ielts_writing",
                        "ielts_speaking",
                    ):
                        out[slot] = mb
                    break

    # Pattern B — explicit Listening/Reading/Writing/Speaking (overrides A).
    mb = _PATTERN_B.search(scan_text)
    if mb:
        try:
            out["ielts_listening"] = float(mb.group(1))
            out["ielts_reading"] = float(mb.group(2))
            out["ielts_writing"] = float(mb.group(3))
            out["ielts_speaking"] = float(mb.group(4))
        except ValueError:
            pass

    # Pattern C — short form "L X.X R X.X W X.X S X.X".
    if "ielts_listening" not in out:
        mc = _PATTERN_C.search(scan_text)
        if mc:
            try:
                out["ielts_listening"] = float(mc.group(1))
                out["ielts_reading"] = float(mc.group(2))
                out["ielts_writing"] = float(mc.group(3))
                out["ielts_speaking"] = float(mc.group(4))
            except ValueError:
                pass

    return out


def extract_modal_english(
    html: str,
    *,
    course_name: str = "",
    degree_level: str = "",
) -> dict[str, Any]:
    """Scan ``html`` for an English-requirements modal and return a dict
    of normalized fields.

    Returns ``{}`` when no modal is found / no scores parse — caller
    should treat the empty dict as "no-op" identically to the
    "extractor disabled" case.
    """
    if not html or "modal" not in html.lower() and "dialog" not in html.lower() and "popup" not in html.lower() and "lightbox" not in html.lower():
        return {}

    modal_html = _find_modal_html(html)
    if not modal_html:
        return {}

    modal_soup = BeautifulSoup(modal_html, "html.parser")
    target_ielts = _select_target_ielts(course_name, degree_level)

    all_rows: list[dict[str, float | int]] = []
    for tbl in modal_soup.find_all("table"):
        if not _MODAL_KEYWORD_RE.search(tbl.get_text(" ", strip=True)):
            continue
        for tr in tbl.find_all("tr"):
            row_vals: list[float] = []
            for cell in tr.find_all(["th", "td"]):
                txt = re.sub(r"\s+", " ", cell.get_text(" ", strip=True))
                if _NUMERIC_CELL_RE.match(txt):
                    try:
                        row_vals.append(float(txt))
                    except ValueError:
                        continue
            if not row_vals:
                continue
            classified = _classify_row_numbers(row_vals)
            if classified:
                all_rows.append(classified)

    if not all_rows:
        return {}

    # Pick the row whose IELTS is closest to ``target_ielts``. If no row
    # has an IELTS value, fall back to the first row.
    rows_with_ielts = [r for r in all_rows if "ielts_overall" in r]
    if rows_with_ielts:
        best = min(
            rows_with_ielts,
            key=lambda r: abs(float(r["ielts_overall"]) - target_ielts),
        )
    else:
        best = all_rows[0]

    out: dict[str, Any] = dict(best)

    # ── Sub-band extraction ─────────────────────────────────────────────
    # Pull from the modal text PLUS the surrounding page body — VIT often
    # places the "no individual band below" sentence in the paragraph
    # next to the modal trigger rather than inside the modal itself.
    full_soup = BeautifulSoup(html, "html.parser")
    modal_text = re.sub(r"\s+", " ", modal_soup.get_text(" ", strip=True))
    page_text = re.sub(r"\s+", " ", full_soup.get_text(" ", strip=True))
    scan_text = f"{modal_text} {page_text}"

    if "ielts_overall" in out:
        sub = _extract_sub_bands(scan_text)
        for k, v in sub.items():
            out.setdefault(k, v)

    # Build the human-readable summary the orchestrator logs.
    sub_log_parts = []
    if "ielts_listening" in out:
        sub_log_parts.append(f"L={out['ielts_listening']}")
    if "ielts_reading" in out:
        sub_log_parts.append(f"R={out['ielts_reading']}")
    if "ielts_writing" in out:
        sub_log_parts.append(f"W={out['ielts_writing']}")
    if "ielts_speaking" in out:
        sub_log_parts.append(f"S={out['ielts_speaking']}")
    sub_log = " ".join(sub_log_parts) or "no sub-bands"

    def _fmt(k: str) -> str:
        v = out.get(k)
        return str(v) if v not in (None, "", 0) else "-"

    out["__modal_summary"] = (
        f"IELTS={_fmt('ielts_overall')} ({sub_log}) "
        f"PTE={_fmt('pte_overall')} "
        f"TOEFL={_fmt('toefl_overall')} "
        f"CAE={_fmt('cambridge_overall')}"
    )
    return out


__all__ = ("extract_modal_english",)
