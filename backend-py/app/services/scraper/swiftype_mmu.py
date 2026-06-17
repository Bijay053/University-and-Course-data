"""Swiftype REST API provider for Manchester Metropolitan University (MMU).

MMU's course catalogue is served through a Swiftype-hosted search engine whose
public API key is embedded in the course-search page JavaScript.  The live
search page (www.mmu.ac.uk/study/course-search/search/) is a Nuxt SPA; the
underlying API endpoint is fully public.

This provider paginates the Swiftype API with ``filters.page.type=course``,
which returns exactly 393 structured records each carrying:
  - Metadata: title, award, level, study_mode, start_date, department_name,
    subjects, ucas_code, year.
  - ``body``: full concatenated page text (~9 KB) containing fees, IELTS
    scores, duration, entry requirements.

Since we extract everything from the API response, no per-course HTTP fetch
is needed — zero Cloudflare exposure.

Each record is mapped to the ``{name, url, searchstax_result}`` link shape
that ``orchestrator._extract_only`` short-circuits on, so the prebuilt result
flows directly into the dedup + staging loop exactly like SearchStax records.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Coroutine, Optional

import httpx

log = logging.getLogger("scraper.swiftype_mmu")

_SWIFTYPE_SEARCH_URL = (
    "https://search-api.swiftype.com/api/v1/public/engines/search.json"
)

# ── IELTS extraction ─────────────────────────────────────────────────────────
_IELTS_OVERALL_RE = re.compile(
    r"IELTS\s+score\s+([\d.]+)",
    re.IGNORECASE,
)
_IELTS_EACH_RE = re.compile(
    r"(?:no\s+component\s+below|minimum\s+(?:of\s+)?|no\s+element\s+(?:below|less\s+than)\s*)"
    r"([\d.]+)",
    re.IGNORECASE,
)

# ── International fee extraction ─────────────────────────────────────────────
# Matches "Overseas £22,000" or "Overseas £9,790" in the "Typical annual fees"
# or "EU and non-EU international students" sections.
_INTL_FEE_RE = re.compile(
    r"(?:Overseas|International)\s+£([\d,]+)",
    re.IGNORECASE,
)

# ── Duration derivation from award type ──────────────────────────────────────
# MMU body text doesn't expose duration in a parseable position; derive from
# the degree type which is highly consistent for taught programmes.
_AWARD_DURATION: list[tuple[re.Pattern, tuple[float, str]]] = [
    (re.compile(r"\bPhD\b|\bDoctorate\b|\bDoctoral\b", re.I),         (3.0, "Years")),
    (re.compile(r"\bMBA\b|\bMRes\b|\bMPhil\b",           re.I),        (1.0, "Years")),
    (re.compile(r"\bMSc\b|\bMA\b|\bMEng\b|\bLLM\b|\bMFA\b|\bMPA\b|\bMPH\b", re.I), (1.0, "Years")),
    (re.compile(r"\bPGDip\b|\bPGCert\b",                  re.I),       (1.0, "Years")),
    (re.compile(r"\bHND\b|\bHNC\b|\bFoundation Degree\b", re.I),       (2.0, "Years")),
    (re.compile(r"\bBSc\b|\bBA\b|\bBEng\b|\bBMus\b|\bBFA\b|\bBNurs\b|\bBMid\b|\bBSW\b", re.I), (3.0, "Years")),
    (re.compile(r"\bLLB\b",                               re.I),        (3.0, "Years")),
]

# ── Degree-level keyword → canonical string ──────────────────────────────────
_LEVEL_MAP = {
    "undergraduate": "Undergraduate",
    "postgraduate":  "Postgraduate",
    "doctorate":     "Doctorate",
    "research":      "Postgraduate",
}

# ── Study-mode normalisation ─────────────────────────────────────────────────
def _study_mode(raw) -> Optional[str]:
    """Normalise Swiftype study_mode → 'Full Time' | 'Part Time' | None."""
    if isinstance(raw, list):
        raw = " ".join(str(x) for x in raw)
    if not raw:
        return None
    s = str(raw).lower()
    if "full" in s:
        return "Full Time"
    if "part" in s:
        return "Part Time"
    return None


# ── Intake months from ISO start_date ────────────────────────────────────────
_MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

def _intake_months(raw) -> list[str]:
    """Extract unique month names from Swiftype start_date field."""
    if not raw:
        return []
    dates = raw if isinstance(raw, list) else [raw]
    months: list[str] = []
    seen: set[int] = set()
    for d in dates:
        m = re.search(r"(\d{4})-(\d{2})-\d{2}", str(d))
        if m:
            month_num = int(m.group(2))
            if 1 <= month_num <= 12 and month_num not in seen:
                months.append(_MONTH_NAMES[month_num])
                seen.add(month_num)
    return months


# ── Duration from award string ────────────────────────────────────────────────
def _duration_from_award(award: str) -> tuple[Optional[float], Optional[str]]:
    """Return (value, term) derived from the award title."""
    for pattern, (val, term) in _AWARD_DURATION:
        if pattern.search(award):
            return val, term
    return None, None


# ── IELTS extraction from body text ──────────────────────────────────────────
def _extract_ielts(body: str) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """Return (ielts_overall, ielts_each_component, snippet) from body text."""
    m_overall = _IELTS_OVERALL_RE.search(body)
    if not m_overall:
        return None, None, None
    try:
        overall = float(m_overall.group(1))
    except ValueError:
        return None, None, None
    # Look for per-component floor in the surrounding 200 chars
    window = body[m_overall.start(): m_overall.start() + 300]
    m_each = _IELTS_EACH_RE.search(window)
    each: Optional[float] = None
    if m_each:
        try:
            each = float(m_each.group(1))
            if each >= overall:   # guard: component floor shouldn't exceed overall
                each = None
        except ValueError:
            each = None
    snippet = body[m_overall.start(): m_overall.start() + 150].strip()
    return overall, each, snippet


# ── International fee extraction from body text ───────────────────────────────
def _extract_fee(body: str) -> Optional[int]:
    """Return the international tuition fee (GBP, int) from body text."""
    m = _INTL_FEE_RE.search(body)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


# ── Entry requirement extraction from body text ───────────────────────────────
_ENTRY_ANCHORS = [
    "Typical offer",
    "Entry requirements",
]

def _extract_entry_req(body: str) -> Optional[str]:
    """Extract a short entry-requirement snippet from body text."""
    for anchor in _ENTRY_ANCHORS:
        idx = body.find(anchor)
        if idx >= 0:
            snippet = body[idx + len(anchor): idx + len(anchor) + 200].strip()
            # Trim at the next section heading (all-caps word or common heading)
            snippet = re.sub(r"\s+(Course length|Typical annual|UCAS|View full|Course overview|IELTS).*", "", snippet)
            snippet = snippet.strip()
            if len(snippet) > 10:
                return snippet[:200]
    return None


# ── Description extraction from body text ─────────────────────────────────────
_DESC_ANCHORS = ["Course overview", "Course information", "Overview"]

def _extract_description(body: str) -> Optional[str]:
    """Extract a short course description from body text."""
    for anchor in _DESC_ANCHORS:
        idx = body.find(anchor)
        if idx >= 0:
            raw = body[idx + len(anchor): idx + len(anchor) + 800].strip()
            # Re-insert spaces at sentence boundaries lost in text extraction
            raw = re.sub(r"([.!?])([A-Z])", r"\1 \2", raw)
            # Trim at the first new section heading
            cut = re.search(r"\n[A-Z][a-z]|Features and benefits|Why study|Accreditation", raw)
            if cut and cut.start() > 80:
                raw = raw[: cut.start()]
            raw = raw.strip()
            if len(raw) > 50:
                return raw[:600]
    return None


# ── Academic level normalisation ──────────────────────────────────────────────
def _academic_level(level_raw: Optional[str]) -> Optional[str]:
    if not level_raw:
        return None
    return _LEVEL_MAP.get(str(level_raw).lower().strip(), None)


# ── Evidence row builder ──────────────────────────────────────────────────────
def _ev(
    field_key: str,
    value: Any,
    method: str,
    source_url: str,
    page_type: str,
    snippet: str,
    confidence: float,
) -> dict:
    return {
        "field_key":       field_key,
        "value":           value,
        "normalized":      value,
        "source_url":      source_url,
        "page_type":       page_type,
        "method":          method,
        "snippet":         snippet,
        "confidence":      confidence,
        "decision_status": "selected",
    }


# ── Main record mapper ────────────────────────────────────────────────────────
def _map_record(rec: dict, cfg: Any) -> Optional[dict]:
    """Map one Swiftype record → ``{name, url, searchstax_result}`` link dict.

    Returns None for records that cannot be mapped to a valid staged course.
    """
    url   = (rec.get("url") or "").strip()
    title = (rec.get("title") or "").strip()
    if not url or not title:
        log.debug("[SWIFTYPE] skip (no url or title): %r", title)
        return None

    award        = (rec.get("award") or "").strip()
    level_raw    = rec.get("level")
    study_mode_r = rec.get("study_mode")
    start_date_r = rec.get("start_date")
    dept         = (rec.get("department_name") or "").strip()
    subjects_r   = rec.get("subjects")
    year         = rec.get("year")
    body         = rec.get("body") or ""

    # Skip courses without a structured URL on the main domain
    # (e.g. fashioninstitute.mmu.ac.uk stubs with no award/level)
    if not award and not level_raw:
        log.debug("[SWIFTYPE] skip (no award/level — likely stub): %r", title)
        return None

    # ── Field extraction ──────────────────────────────────────────────────────
    name        = title
    degree_lvl  = award if award else title.split()[0]  # fallback: first token
    acad_level  = _academic_level(level_raw if isinstance(level_raw, str) else None)
    mode        = _study_mode(study_mode_r)
    intakes     = _intake_months(start_date_r)
    dur_val, dur_term = _duration_from_award(award)
    category    = subjects_r if isinstance(subjects_r, str) else (
        subjects_r[0] if isinstance(subjects_r, list) and subjects_r else None
    )

    ielts_overall, ielts_each, ielts_snippet = _extract_ielts(body)
    fee_val   = _extract_fee(body)
    entry_req = _extract_entry_req(body)
    desc      = _extract_description(body)

    city = getattr(cfg, "city", "Manchester")
    currency = getattr(cfg, "currency", "GBP")
    fee_year = int(getattr(cfg, "fee_year", 2025))

    # ── Build payload ─────────────────────────────────────────────────────────
    payload: dict[str, Any] = {
        "course_name":   name,
        "degree_level":  degree_lvl,
        "course_location": city,
    }
    if acad_level:
        payload["academic_level"] = acad_level
    if mode:
        payload["study_mode"] = mode
    if intakes:
        payload["intake_months"] = ", ".join(intakes)
    if dur_val is not None:
        payload["duration"]      = dur_val
        payload["duration_term"] = dur_term
    if fee_val is not None:
        payload["international_fee"] = float(fee_val)
        payload["fee_term"]          = "Year"
        payload["fee_year"]          = fee_year
        payload["currency"]          = currency
    if ielts_overall is not None:
        payload["ielts_overall"] = ielts_overall
        if ielts_each is not None:
            payload["ielts_listening"] = ielts_each
            payload["ielts_reading"]   = ielts_each
            payload["ielts_writing"]   = ielts_each
            payload["ielts_speaking"]  = ielts_each
    if category:
        payload["category"] = category
    if dept:
        payload["sub_category"] = dept
    if entry_req:
        payload["other_requirement"] = entry_req
    if desc:
        payload["description"] = desc

    # ── Build evidence list ───────────────────────────────────────────────────
    evidence: list[dict] = []

    evidence.append(_ev(
        "course_name", name, "swiftype:title", url, "course",
        f"Swiftype title: {title}", 0.95,
    ))
    evidence.append(_ev(
        "degree_level", degree_lvl, "swiftype:award", url, "course",
        f"Swiftype award field: {award!r}", 0.9,
    ))
    evidence.append(_ev(
        "course_location", city, "swiftype:hardcoded", url, "course",
        f"All MMU courses delivered in {city} (or online).", 0.99,
    ))

    if acad_level:
        evidence.append(_ev(
            "academic_level", acad_level, "swiftype:level", url, "course",
            f"Swiftype level field: {level_raw!r}", 0.95,
        ))
    if mode:
        evidence.append(_ev(
            "study_mode", mode, "swiftype:study_mode", url, "course",
            f"Swiftype study_mode field: {study_mode_r!r}", 0.85,
        ))
    if intakes:
        evidence.append(_ev(
            "intake_months", ", ".join(intakes), "swiftype:start_date", url, "course",
            f"Swiftype start_date field: {start_date_r!r}", 0.85,
        ))
    if dur_val is not None:
        evidence.append(_ev(
            "duration", dur_val, "swiftype:award_derived_duration", url, "course",
            f"Duration {dur_val} {dur_term} derived from award type: {award!r}", 0.7,
        ))
    if fee_val is not None:
        evidence.append(_ev(
            "international_fee", fee_val, "swiftype:body_fee", url, "course",
            f"International fee £{fee_val:,} extracted from body text (Typical annual fees).", 0.85,
        ))
    if ielts_overall is not None:
        evidence.append(_ev(
            "ielts_overall", ielts_overall, "swiftype:body_ielts", url, "course",
            ielts_snippet or f"IELTS score {ielts_overall}", 0.85,
        ))
        if ielts_each is not None:
            for sub in ("ielts_listening", "ielts_reading", "ielts_writing", "ielts_speaking"):
                evidence.append(_ev(
                    sub, ielts_each, "swiftype:body_ielts", url, "course",
                    f"No component below {ielts_each} (from body text IELTS section).", 0.75,
                ))
    if category:
        evidence.append(_ev(
            "category", category, "swiftype:subjects", url, "course",
            f"Swiftype subjects field: {subjects_r!r}", 0.8,
        ))
    if entry_req:
        evidence.append(_ev(
            "other_requirement", entry_req, "swiftype:body_entry_req", url, "course",
            f"Entry requirement from body text: {entry_req[:100]}", 0.7,
        ))

    result = {"name": name, "url": url, "payload": payload, "evidence": evidence}
    return {"name": name, "url": url, "searchstax_result": result}


# ── Public entry point ────────────────────────────────────────────────────────
EmitFn = Callable[..., Coroutine]


async def fetch_swiftype_links(
    cfg: Any,
    emit: Optional[EmitFn] = None,
) -> list[dict]:
    """Paginate the Swiftype public search API and return mapped course links.

    Args:
        cfg: A ``SwiftypeConfig`` instance from the uni YAML.
        emit: Optional coroutine for streaming status messages.

    Returns:
        List of ``{name, url, searchstax_result}`` dicts (same shape as
        SearchStax provider output, consumed by ``_extract_only``).
    """

    async def _emit(msg: str) -> None:
        if emit:
            try:
                await emit("status", msg, phase="discover")
            except Exception:  # noqa: BLE001
                pass

    engine_key  = cfg.engine_key
    search_url  = getattr(cfg, "search_url", _SWIFTYPE_SEARCH_URL)
    per_page    = int(getattr(cfg, "per_page", 100))
    type_filter = getattr(cfg, "type_filter", "course")

    if not engine_key:
        log.error("[SWIFTYPE] engine_key not set — aborting")
        return []

    await _emit(f"[SWIFTYPE] Querying Swiftype engine (type={type_filter}) ...")

    links: list[dict] = []
    skipped = 0
    page = 1
    total: Optional[int] = None

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            payload: dict[str, Any] = {
                "engine_key": engine_key,
                "q":          "",
                "per_page":   per_page,
                "page":       page,
                "filters": {
                    "page": {"type": type_filter},
                },
            }
            try:
                resp = await client.post(search_url, json=payload)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                log.error("[SWIFTYPE] fetch failed (page=%d): %s", page, exc)
                break

            records: list[dict] = data.get("records", {}).get("page", [])
            info    = data.get("info", {}).get("page", {})

            if total is None:
                total = int(info.get("total_result_count", 0))
                num_pages = int(info.get("num_pages", 1))
                log.info(
                    "[SWIFTYPE] %d course records across %d page(s) (per_page=%d)",
                    total, num_pages, per_page,
                )
                await _emit(f"[SWIFTYPE] {total} course records found.")

            if not records:
                break

            for rec in records:
                mapped = _map_record(rec, cfg)
                if mapped is not None:
                    links.append(mapped)
                else:
                    skipped += 1

            log.info(
                "[SWIFTYPE] page %d/%d — mapped %d so far (%d skipped)",
                page, info.get("num_pages", "?"), len(links), skipped,
            )

            current_page  = int(info.get("current_page", page))
            total_pages   = int(info.get("num_pages", 1))
            if current_page >= total_pages:
                break

            page += 1
            await asyncio.sleep(0.1)   # be polite to Swiftype API

    await _emit(
        f"[SWIFTYPE] Built {len(links)} course record(s) "
        f"({skipped} skipped — no award/level)."
    )
    log.info(
        "[SWIFTYPE] total=%s mapped=%s skipped=%s",
        total, len(links), skipped,
    )
    return links
