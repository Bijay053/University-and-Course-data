"""
Three-phase AI Diagnostics for the Recipe Editor.

Phase 1 — Analyse the last completed scrape job (field completion rates, patterns).
Phase 2 — Probe the live site with httpx to detect available data sources.
Phase 3 — Cross-correlate to produce root-cause analysis and actionable recipe fixes.
"""
from __future__ import annotations

import re
import logging
from urllib.parse import urljoin, urlparse
from typing import Any

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# ── Pattern libraries ──────────────────────────────────────────────────────────

_FEE_LINK_RE = re.compile(
    r"(fees?\s+(and\s+)?(scholarships?|information|calculator|schedule|payment)"
    r"|international\s+(student\s+)?fees?"
    r"|fees?\s+for\s+(your|this)\s+course"
    r"|tuition\s+fees?"
    r"|view\s+fees?)",
    re.IGNORECASE,
)

_ENGLISH_LINK_RE = re.compile(
    r"(english\s+(language\s+)?(requirements?|proficiency|entry|criteria)"
    r"|ielts\s+requirements?"
    r"|language\s+requirements?"
    r"|admissions?\s+(policy|requirements?)"
    r"|english\s+entry\s+requirements?)",
    re.IGNORECASE,
)

_ONLINE_DELIVERY_RE = re.compile(
    r"(online\s*:\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"|\bonline\b.*\b(delivery|mode|learning|study)\b"
    r"|delivered\s+online"
    r"|study\s+online)",
    re.IGNORECASE,
)

_CAMPUS_RE = re.compile(
    r"\b(townsville|cairns|brisbane|sydney|melbourne|perth|adelaide|darwin"
    r"|hobart|canberra|gold\s*coast|sunshine\s*coast|newcastle|wollongong"
    r"|geelong|singapore|online)\b",
    re.IGNORECASE,
)

_PROBE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}

# ── Extended extraction-evidence patterns ──────────────────────────────────────

_INTL_FEE_TEXT_RE = re.compile(
    r"(international\s+(student\s+)?(tuition\s+)?fee"
    r"|estimated\s+(annual\s+)?tuition"
    r"|overseas\s+(student\s+)?fee"
    r"|full\s+fee\s+paying"
    r"|tuition\s+fee\s+for\s+international"
    r"|annual\s+tuition\s*[:\-–]\s*\$\d)",
    re.IGNORECASE,
)

_ENGLISH_SECTION_RE = re.compile(
    r"(english\s+(language\s+)?(requirement|proficiency|entry|criteria|standard)"
    r"|minimum\s+english"
    r"|english\s+entry\s+requirement"
    r"|ielts\s+(overall|score|minimum|requirement)"
    r"|language\s+(proficiency\s+)?requirement)",
    re.IGNORECASE,
)

_IELTS_COMPONENT_RE = re.compile(
    r"\b(listening|reading|writing|speaking)\s*[:\-–]\s*\d",
    re.IGNORECASE,
)

_IELTS_OVERALL_TEXT_RE = re.compile(
    r"ielts\s*(overall|band)?\s*[:\-–]?\s*\d+\.\d",
    re.IGNORECASE,
)

_ENGLISH_LINK_PAGE_RE = re.compile(
    r"(english\s+(language\s+)?(requirements?|entry|proficiency)"
    r"|ielts\s+requirements?"
    r"|language\s+requirements?)",
    re.IGNORECASE,
)


# ── Phase 1: DB analysis ───────────────────────────────────────────────────────

async def _analyse_last_job(uni_id: int, db: AsyncSession) -> dict[str, Any]:
    """Query the most recent completed scrape job and calculate field completion rates."""

    job_row = (await db.execute(text("""
        SELECT runtime_job_id, total_found, imported, errors, completed_at
        FROM   scrape_runtime_jobs
        WHERE  university_id = :uid AND status = 'completed'
        ORDER  BY completed_at DESC NULLS LAST
        LIMIT  1
    """), {"uid": uni_id})).fetchone()

    if job_row is None:
        return {"status": "no_completed_job"}

    job_id, total_found, imported, errors, completed_at = job_row

    cmp = (await db.execute(text("""
        SELECT
            COUNT(*)                                                                  AS total,
            COUNT(international_fee) FILTER (WHERE international_fee > 0)            AS has_fee,
            COUNT(ielts_overall)     FILTER (WHERE ielts_overall > 0)                AS has_ielts,
            COUNT(pte_overall)       FILTER (WHERE pte_overall > 0)                  AS has_pte,
            COUNT(toefl_overall)     FILTER (WHERE toefl_overall > 0)                AS has_toefl,
            COUNT(study_mode)        FILTER (WHERE study_mode IS NOT NULL
                                               AND study_mode <> '')                 AS has_study_mode,
            COUNT(degree_level)      FILTER (WHERE degree_level IS NOT NULL
                                               AND degree_level <> '')               AS has_degree_level,
            COUNT(duration)          FILTER (WHERE duration > 0)                     AS has_duration,
            COUNT(academic_level)    FILTER (WHERE academic_level IS NOT NULL
                                               AND academic_level <> '')             AS has_academic_level,
            COUNT(*)                 FILTER (WHERE intake_months IS NOT NULL
                                               AND intake_months::text
                                               NOT IN ('null','[]',''))              AS has_intake,
            COUNT(*)                 FILTER (WHERE study_mode = 'Online')            AS sm_online,
            COUNT(*)                 FILTER (WHERE study_mode = 'On Campus')         AS sm_on_campus,
            COUNT(*)                 FILTER (WHERE study_mode = 'Blended')           AS sm_blended,
            COUNT(*)                 FILTER (WHERE international_fee IS NOT NULL
                                               AND international_fee BETWEEN 1 AND 9999) AS suspiciously_low_fee,
            -- ── Quality: filled but correct? ──────────────────────────────────
            COUNT(international_fee) FILTER (WHERE international_fee >= 10000)       AS good_fee,
            COUNT(ielts_overall)     FILTER (WHERE ielts_overall BETWEEN 4.0 AND 8.5) AS good_ielts,
            COUNT(course_location)   FILTER (
                WHERE course_location IS NOT NULL AND course_location <> ''
                AND   length(course_location) > 2
                AND   course_location !~* '\\y(not|this|internal|mixed|tba|tbd|n/?a|unknown|online only)\\y'
            )                                                                        AS good_location,
            COUNT(course_location)   FILTER (WHERE course_location IS NOT NULL
                                               AND course_location <> '')            AS has_location,
            COUNT(study_mode)        FILTER (
                WHERE study_mode IN ('On Campus','Online','Blended',
                                     'On Campus/Online','Distance','Flexible')
            )                                                                        AS good_study_mode
        FROM scraped_courses
        WHERE scrape_job_id = :jid
    """), {"jid": job_id})).fetchone()

    total = int(cmp[0] or 1)

    def _pct(n: Any) -> float:
        return round(int(n or 0) / total, 3) if total else 0.0

    def _quality(filled: Any, good: Any) -> float:
        f = int(filled or 0)
        return round(int(good or 0) / f, 3) if f else 1.0

    field_completion = {
        "international_fee": {
            "count": int(cmp[1] or 0),
            "missing": total - int(cmp[1] or 0),
            "pct": _pct(cmp[1]),
            "quality_pct": _quality(cmp[1], cmp[14]),
            "quality_issues": int(cmp[1] or 0) - int(cmp[14] or 0),
            "quality_label": "Fee ≥ $10,000 (not domestic)",
        },
        "ielts_overall": {
            "count": int(cmp[2] or 0),
            "missing": total - int(cmp[2] or 0),
            "pct": _pct(cmp[2]),
            "quality_pct": _quality(cmp[2], cmp[15]),
            "quality_issues": int(cmp[2] or 0) - int(cmp[15] or 0),
            "quality_label": "IELTS in valid range 4.0–8.5",
        },
        "pte_overall": {
            "count": int(cmp[3] or 0),
            "missing": total - int(cmp[3] or 0),
            "pct": _pct(cmp[3]),
        },
        "toefl_overall": {
            "count": int(cmp[4] or 0),
            "missing": total - int(cmp[4] or 0),
            "pct": _pct(cmp[4]),
        },
        "study_mode": {
            "count": int(cmp[5] or 0),
            "missing": total - int(cmp[5] or 0),
            "pct": _pct(cmp[5]),
            "quality_pct": _quality(cmp[5], cmp[18]),
            "quality_issues": int(cmp[5] or 0) - int(cmp[18] or 0),
            "quality_label": "Recognised mode value",
        },
        "degree_level": {
            "count": int(cmp[6] or 0),
            "missing": total - int(cmp[6] or 0),
            "pct": _pct(cmp[6]),
        },
        "duration": {
            "count": int(cmp[7] or 0),
            "missing": total - int(cmp[7] or 0),
            "pct": _pct(cmp[7]),
        },
        "academic_level": {
            "count": int(cmp[8] or 0),
            "missing": total - int(cmp[8] or 0),
            "pct": _pct(cmp[8]),
        },
        "intake_months": {
            "count": int(cmp[9] or 0),
            "missing": total - int(cmp[9] or 0),
            "pct": _pct(cmp[9]),
        },
        "course_location": {
            "count": int(cmp[17] or 0),
            "missing": total - int(cmp[17] or 0),
            "pct": _pct(cmp[17]),
            "quality_pct": _quality(cmp[17], cmp[16]),
            "quality_issues": int(cmp[17] or 0) - int(cmp[16] or 0),
            "quality_label": "Location not garbage (no 'Not', 'TBA', 'Internal')",
        },
    }

    # ── Top 10 most broken courses ─────────────────────────────────────────────
    broken_rows = (await db.execute(text("""
        SELECT
            course_name,
            international_fee,
            ielts_overall,
            course_location,
            study_mode,
            degree_level,
            -- Issue flags as integer scores for ranking
            CASE WHEN international_fee IS NULL OR international_fee = 0 THEN 2
                 WHEN international_fee BETWEEN 1 AND 9999            THEN 1
                 ELSE 0 END                                           AS fee_score,
            CASE WHEN ielts_overall IS NULL OR ielts_overall = 0      THEN 2 ELSE 0 END AS ielts_score,
            CASE WHEN course_location IS NULL OR course_location = ''  THEN 1
                 WHEN course_location ~* '\\y(not|this|internal|mixed|tba|tbd)\\y' THEN 1
                 ELSE 0 END                                           AS loc_score,
            CASE WHEN study_mode IS NULL OR study_mode = ''           THEN 1 ELSE 0 END AS mode_score,
            CASE WHEN degree_level IS NULL OR degree_level = ''       THEN 1 ELSE 0 END AS deg_score
        FROM scraped_courses
        WHERE scrape_job_id = :jid
        ORDER BY (
            CASE WHEN international_fee IS NULL OR international_fee = 0 THEN 2
                 WHEN international_fee BETWEEN 1 AND 9999 THEN 1 ELSE 0 END +
            CASE WHEN ielts_overall IS NULL OR ielts_overall = 0 THEN 2 ELSE 0 END +
            CASE WHEN course_location IS NULL OR course_location = '' THEN 1
                 WHEN course_location ~* '\\y(not|this|internal|mixed|tba|tbd)\\y' THEN 1
                 ELSE 0 END +
            CASE WHEN study_mode IS NULL OR study_mode = '' THEN 1 ELSE 0 END +
            CASE WHEN degree_level IS NULL OR degree_level = '' THEN 1 ELSE 0 END
        ) DESC, course_name
        LIMIT 10
    """), {"jid": job_id})).fetchall()

    top_broken_courses = []
    for row in broken_rows:
        name, fee, ielts, loc, mode, deg, fee_s, ielts_s, loc_s, mode_s, deg_s = row
        total_score = fee_s + ielts_s + loc_s + mode_s + deg_s
        if total_score == 0:
            continue  # course is fine — stop once we reach clean courses
        issues = []
        if fee_s == 2:
            issues.append({"field": "international_fee", "label": "Fee missing", "severity": "critical"})
        elif fee_s == 1:
            issues.append({"field": "international_fee", "label": f"Domestic fee (${int(fee):,})", "severity": "critical"})
        if ielts_s == 2:
            issues.append({"field": "ielts_overall", "label": "IELTS missing", "severity": "critical"})
        if loc_s == 1:
            if loc:
                issues.append({"field": "course_location", "label": f"Invalid location: {loc[:40]}", "severity": "warning"})
            else:
                issues.append({"field": "course_location", "label": "Location missing", "severity": "warning"})
        if mode_s:
            issues.append({"field": "study_mode", "label": "Study mode missing", "severity": "warning"})
        if deg_s:
            issues.append({"field": "degree_level", "label": "Degree level missing", "severity": "warning"})
        top_broken_courses.append({
            "course_name": name or "(unnamed)",
            "score": total_score,
            "issues": issues,
        })

    return {
        "status": "ok",
        "job_id": job_id,
        "completed_at": completed_at.isoformat() if completed_at else None,
        "total_found": int(total_found or 0),
        "imported": int(imported or 0),
        "errors": int(errors or 0),
        "courses_analysed": total,
        "field_completion": field_completion,
        "study_mode_breakdown": {
            "Online": int(cmp[10] or 0),
            "On Campus": int(cmp[11] or 0),
            "Blended": int(cmp[12] or 0),
        },
        "suspiciously_low_fee_count": int(cmp[13] or 0),
        "top_broken_courses": top_broken_courses,
    }


# ── Phase 2: Live site probe ───────────────────────────────────────────────────

async def _probe_live_site(uni_id: int, db: AsyncSession) -> dict[str, Any]:
    """Probe configured seed URLs with httpx to detect available data sources."""

    uni_row = (await db.execute(text("""
        SELECT scrape_url, scrape_config, website, name
        FROM   universities WHERE id = :uid
    """), {"uid": uni_id})).fetchone()

    if uni_row is None:
        return {"status": "no_university"}

    scrape_url, scrape_config, website, uni_name = uni_row
    sc = scrape_config or {}
    recipe = sc.get("recipe") or sc.get("admin_config") or {}

    seed_urls: list[str] = (
        recipe.get("seed_urls")
        or (sc.get("auto_config") or {}).get("seed_urls")
        or ([scrape_url] if scrape_url else [])
        or ([website] if website else [])
    )
    seed_urls = [u for u in seed_urls if u][:3]

    if not seed_urls:
        return {"status": "no_seed_urls", "urls_probed": []}

    fee_link_texts: list[str] = []
    english_link_texts: list[str] = []
    fee_page_urls: list[str] = []
    english_page_urls: list[str] = []
    pdf_urls: list[str] = []
    json_api_hints: list[str] = []
    campus_names: list[str] = []
    has_online_delivery = False
    has_tab_layout = False
    cloudflare_blocked = False
    probed_urls: list[str] = []

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=12,
        headers=_PROBE_HEADERS,
    ) as client:
        for url in seed_urls[:2]:
            try:
                resp = await client.get(url)
                probed_urls.append(url)

                # Detect Cloudflare block
                if resp.status_code in (403, 429, 503) or b"cf-ray" in resp.headers.get("cf-ray", "").encode():
                    cloudflare_blocked = True
                    continue

                if resp.status_code != 200:
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                page_text = soup.get_text(" ", strip=True)

                # Tab layout detection
                if (soup.find(attrs={"role": "tablist"})
                        or soup.find(class_=re.compile(r"\btab(s|-panel|-content|-list)?\b", re.I))):
                    has_tab_layout = True

                # Online delivery detection
                if _ONLINE_DELIVERY_RE.search(page_text[:8000]):
                    has_online_delivery = True

                # Campus name extraction
                for m in _CAMPUS_RE.finditer(page_text[:5000]):
                    name = m.group(0).strip().title()
                    if name not in campus_names:
                        campus_names.append(name)

                base_domain = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

                for a in soup.find_all("a", href=True)[:600]:
                    href = str(a.get("href", ""))
                    link_text = a.get_text(" ", strip=True)[:120]
                    abs_href = urljoin(base_domain, href) if href.startswith("/") else urljoin(url, href)

                    if _FEE_LINK_RE.search(link_text):
                        if link_text not in fee_link_texts:
                            fee_link_texts.append(link_text)
                        if abs_href not in fee_page_urls:
                            fee_page_urls.append(abs_href)

                    if _ENGLISH_LINK_RE.search(link_text):
                        if link_text not in english_link_texts:
                            english_link_texts.append(link_text)
                        if abs_href not in english_page_urls:
                            english_page_urls.append(abs_href)

                    if href.lower().endswith(".pdf") and abs_href not in pdf_urls:
                        pdf_urls.append(abs_href)

                    if ("/api/" in href or href.endswith(".json") or "graphql" in href.lower()):
                        if abs_href not in json_api_hints:
                            json_api_hints.append(abs_href)

            except Exception as exc:
                log.warning("Probe failed for %s: %s", url, exc)

    return {
        "status": "ok",
        "urls_probed": probed_urls,
        "cloudflare_blocked": cloudflare_blocked,
        "detected": {
            "fee_link_texts": fee_link_texts[:8],
            "fee_page_urls": fee_page_urls[:5],
            "english_link_texts": english_link_texts[:8],
            "english_page_urls": english_page_urls[:5],
            "pdf_urls": pdf_urls[:5],
            "json_api_hints": json_api_hints[:5],
            "has_tab_layout": has_tab_layout,
            "has_online_delivery": has_online_delivery,
            "campus_names": campus_names[:10],
        },
    }


# ── Phase 1b: Course-level pattern analysis ───────────────────────────────────

_GARBAGE_LOCATION_RE = re.compile(
    r"\b(not\s+available|not\b|this\b|internal\b|mixed\b|tba\b|tbd\b|n/?a\b|online\s+only\b)\b",
    re.IGNORECASE,
)
_BAND_TEXT_RE = re.compile(r"\bband\s+\d\b", re.IGNORECASE)
_FEE_AMOUNT_RE = re.compile(r"\$[\d,]+|\d[\d,]+\s*(AUD|USD|GBP|per\s+year|per\s+semester)", re.IGNORECASE)
_CSP_TEXT_RE = re.compile(
    r"(commonwealth\s+supported|CSP\b|HECS[-\s]HELP|domestic\s+fee|local\s+fee)", re.IGNORECASE
)


async def _analyse_course_patterns(
    job_id: str, uni_id: int, db: AsyncSession
) -> dict[str, Any]:
    """Analyse course-level patterns: name pollution, location garbage, band gaps, etc."""

    # Fetch university name and scrape_config for band_mapping check
    uni_row = (await db.execute(text(
        "SELECT name, scrape_config FROM universities WHERE id = :uid"
    ), {"uid": uni_id})).fetchone()

    uni_name = uni_row[0] if uni_row else ""
    sc = (uni_row[1] or {}) if uni_row else {}
    recipe = sc.get("recipe") or {}
    admin_config = sc.get("admin_config") or {}

    band_mapping_configured = bool(
        recipe.get("band_mapping") or admin_config.get("band_mapping")
    )

    # ── Main pattern query ─────────────────────────────────────────────────────
    pat = (await db.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE course_name LIKE '%|%')       AS pipe_suffix_count,
            COUNT(*) FILTER (
                WHERE course_location IS NOT NULL
                  AND course_location ~* '\\y(not|this|internal|mixed|tba|tbd)\\y'
            )                                                    AS garbage_location_count,
            COUNT(*) FILTER (WHERE international_fee BETWEEN 1 AND 9999) AS low_fee_count
        FROM scraped_courses
        WHERE scrape_job_id = :jid
    """), {"jid": job_id})).fetchone()

    pipe_suffix_count = int(pat[0] or 0)
    garbage_location_count = int(pat[1] or 0)
    low_fee_count = int(pat[2] or 0)

    # ── Sample garbage locations ───────────────────────────────────────────────
    sample_garbage = []
    if garbage_location_count > 0:
        g_rows = (await db.execute(text("""
            SELECT DISTINCT course_location FROM scraped_courses
            WHERE scrape_job_id = :jid
              AND course_location IS NOT NULL
              AND course_location ~* '\\y(not|this|internal|mixed|tba|tbd)\\y'
            LIMIT 5
        """), {"jid": job_id})).fetchall()
        sample_garbage = [r[0] for r in g_rows if r[0]]

    # ── Sample course URLs where key fields are blank (for Phase 2 probing) ───
    blank_ielts_urls: list[str] = []
    blank_fee_urls: list[str] = []

    ielts_urls_rows = (await db.execute(text("""
        SELECT course_website FROM scraped_courses
        WHERE scrape_job_id = :jid
          AND ielts_overall IS NULL
          AND course_website IS NOT NULL
          AND course_website LIKE 'http%'
        LIMIT 3
    """), {"jid": job_id})).fetchall()
    blank_ielts_urls = [r[0] for r in ielts_urls_rows if r[0]]

    fee_urls_rows = (await db.execute(text("""
        SELECT course_website FROM scraped_courses
        WHERE scrape_job_id = :jid
          AND international_fee IS NULL
          AND course_website IS NOT NULL
          AND course_website LIKE 'http%'
        LIMIT 3
    """), {"jid": job_id})).fetchall()
    blank_fee_urls = [r[0] for r in fee_urls_rows if r[0]]

    # ── Name pollution: extract a sample pipe-suffix name ─────────────────────
    sample_pipe_names: list[str] = []
    if pipe_suffix_count > 0:
        pn_rows = (await db.execute(text("""
            SELECT course_name FROM scraped_courses
            WHERE scrape_job_id = :jid AND course_name LIKE '%|%'
            LIMIT 3
        """), {"jid": job_id})).fetchall()
        sample_pipe_names = [r[0] for r in pn_rows if r[0]]

    return {
        "uni_name": uni_name,
        "band_mapping_configured": band_mapping_configured,
        "pipe_suffix_count": pipe_suffix_count,
        "sample_pipe_names": sample_pipe_names,
        "garbage_location_count": garbage_location_count,
        "sample_garbage_locations": sample_garbage,
        "low_fee_count": low_fee_count,
        "blank_ielts_urls": blank_ielts_urls,
        "blank_fee_urls": blank_fee_urls,
    }


# ── Phase 2b: Sample course page probing ──────────────────────────────────────

def _extract_snippet(text: str, pattern: re.Pattern, window: int = 90) -> str | None:
    """Return a short snippet of text around the first match of *pattern*."""
    m = pattern.search(text)
    if not m:
        return None
    start = max(0, m.start() - 20)
    end = min(len(text), m.end() + window)
    snippet = text[start:end].replace("\n", " ").strip()
    return f"…{snippet}…"


async def _probe_sample_course_pages(
    blank_ielts_urls: list[str],
    blank_fee_urls: list[str],
) -> dict[str, Any]:
    """Probe up to 5 blank-IELTS + 5 blank-fee course pages for extraction signals.

    Returns aggregated boolean flags (backwards-compatible) PLUS per-page evidence
    so downstream code can cite specific page text in recommendations.
    """
    ielts_sample = blank_ielts_urls[:5]
    fee_sample = blank_fee_urls[:5]
    all_urls = list(dict.fromkeys(ielts_sample + fee_sample))

    _empty: dict[str, Any] = {
        "probed": 0,
        "band_text_found": False,
        "fee_text_in_blank_pages": False,
        "csp_text_found": False,
        "international_fee_text_found": False,
        "english_section_found": False,
        "english_link_found": False,
        "ielts_overall_text_found": False,
        "ielts_components_text_found": False,
        "cloudflare_blocked_courses": False,
        "per_page": [],
    }
    if not all_urls:
        return _empty

    cloudflare_blocked_courses = False
    per_page: list[dict] = []

    # Aggregated flags
    band_text_found = False
    fee_text_in_blank_pages = False
    csp_text_found = False
    international_fee_text_found = False
    english_section_found = False
    english_link_found = False
    ielts_overall_text_found = False
    ielts_components_text_found = False

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=12,
        headers=_PROBE_HEADERS,
    ) as client:
        for url in all_urls:
            try:
                resp = await client.get(url)
                if resp.status_code in (403, 429, 503):
                    cloudflare_blocked_courses = True
                    continue
                if resp.status_code != 200:
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                text_content = soup.get_text(" ", strip=True)[:10000]

                is_ielts_url = url in ielts_sample
                is_fee_url = url in fee_sample

                signals: dict[str, bool] = {}
                snippets: list[str] = []

                def _check(pattern: re.Pattern, key: str) -> bool:
                    snip = _extract_snippet(text_content, pattern)
                    if snip:
                        signals[key] = True
                        snippets.append(f"[{key}] {snip}")
                        return True
                    return False

                if _check(_BAND_TEXT_RE, "band_text"):
                    if is_ielts_url:
                        band_text_found = True

                if _check(_FEE_AMOUNT_RE, "fee_amount"):
                    if is_fee_url:
                        fee_text_in_blank_pages = True

                if _check(_CSP_TEXT_RE, "csp_domestic_fee"):
                    csp_text_found = True

                if _check(_INTL_FEE_TEXT_RE, "international_fee"):
                    international_fee_text_found = True

                if _check(_ENGLISH_SECTION_RE, "english_section"):
                    english_section_found = True

                if _check(_IELTS_OVERALL_TEXT_RE, "ielts_overall"):
                    ielts_overall_text_found = True

                if _check(_IELTS_COMPONENT_RE, "ielts_components"):
                    ielts_components_text_found = True

                # Check for English follow-links on the page
                for a in soup.find_all("a", href=True)[:300]:
                    link_text = a.get_text(" ", strip=True)
                    if _ENGLISH_LINK_PAGE_RE.search(link_text):
                        signals["english_link"] = True
                        snippets.append(f"[english_link] {link_text[:80]}")
                        english_link_found = True
                        break

                per_page.append({
                    "url": url,
                    "is_ielts_url": is_ielts_url,
                    "is_fee_url": is_fee_url,
                    "signals": signals,
                    "detected_snippets": snippets[:5],
                })

            except Exception as exc:
                log.debug("Sample course probe failed for %s: %s", url, exc)

    return {
        "probed": len(per_page),
        "band_text_found": band_text_found,
        "fee_text_in_blank_pages": fee_text_in_blank_pages,
        "csp_text_found": csp_text_found,
        "international_fee_text_found": international_fee_text_found,
        "english_section_found": english_section_found,
        "english_link_found": english_link_found,
        "ielts_overall_text_found": ielts_overall_text_found,
        "ielts_components_text_found": ielts_components_text_found,
        "cloudflare_blocked_courses": cloudflare_blocked_courses,
        "per_page": per_page[:5],
    }


# ── Phase 3: Cross-correlate and generate recommendations ─────────────────────

def _confidence_reason(
    confidence: float,
    evidence: dict,
    affected_count: int,
    total: int,
    page_probe: dict | None = None,
) -> str:
    """Generate a human-readable explanation for a confidence score.

    Args:
        confidence: 0.0–1.0 float as set on the rec
        evidence: the rec's evidence dict (detected_snippets, page_signals, etc.)
        affected_count: number of courses missing the field
        total: total courses analysed
        page_probe: optional per-page probe entry (is_ielts_url / is_fee_url page)
    """
    parts: list[str] = []
    pct = int(confidence * 100)

    snippets = evidence.get("detected_snippets") or []
    page_signals: dict = evidence.get("page_signals") or {}
    signal_count = sum(1 for v in page_signals.values() if v)
    snippet_count = len(snippets)

    # Affected courses
    if affected_count and total:
        parts.append(f"{affected_count}/{total} courses confirmed missing this field")

    # Live page evidence
    if snippet_count and signal_count:
        sigs = [s.replace("_", " ") for s, v in page_signals.items() if v]
        parts.append(
            f"{snippet_count} text snippet(s) from sampled pages + {signal_count} page signal(s) "
            f"({', '.join(sigs[:3])}) confirm the data exists"
        )
    elif snippet_count:
        parts.append(f"{snippet_count} text snippet(s) from sampled pages confirm the data exists")
    elif signal_count:
        sigs = [s.replace("_", " ") for s, v in page_signals.items() if v]
        parts.append(f"{signal_count} page signal(s) detected: {', '.join(sigs[:3])}")
    else:
        parts.append("Based on field-completion analysis of staged courses — no live page sample available")

    # Calibration note
    if pct >= 90:
        parts.append("High confidence — multiple independent signals agree")
    elif pct >= 75:
        parts.append("Good confidence — primary signal present but limited page sampling")
    elif pct >= 60:
        parts.append("Moderate confidence — limited evidence; verify manually before applying")
    else:
        parts.append("Low confidence — no live page evidence; this is a best-guess recommendation")

    return ". ".join(parts) + "."


def _impact_estimate(
    phase1: dict,
    target_fields: list[str],
    estimated_fill_pct: float = 0.85,
    courses_affected_override: int | None = None,
) -> dict[str, Any]:
    """Estimate the impact of fixing one or more fields.

    Returns:
        courses_affected  — number of courses that would gain data
        overall_before    — current mean completeness across all fields (%)
        overall_after     — estimated mean completeness after the fix (%)
        delta             — expected completeness gain (percentage points)
    """
    fc = phase1.get("field_completion", {})
    total = max(phase1.get("courses_analysed", 1), 1)

    if courses_affected_override is not None:
        courses_affected = courses_affected_override
    else:
        courses_affected = sum(fc.get(f, {}).get("missing", 0) for f in target_fields)

    before_pcts = [v.get("pct", 0.0) for v in fc.values()]
    overall_before = round(sum(before_pcts) / max(len(before_pcts), 1) * 100, 1)

    # Simulate: affected fields rise toward estimated_fill_pct
    after_fc = {k: dict(v) for k, v in fc.items()}
    for f in target_fields:
        if f in after_fc:
            current_pct = after_fc[f].get("pct", 0.0)
            gain = min(estimated_fill_pct, current_pct + (after_fc[f].get("missing", 0) / total))
            after_fc[f]["pct"] = gain

    after_pcts = [v.get("pct", 0.0) for v in after_fc.values()]
    overall_after = round(sum(after_pcts) / max(len(after_pcts), 1) * 100, 1)

    return {
        "courses_affected": courses_affected,
        "overall_completeness_before": overall_before,
        "overall_completeness_after": overall_after,
        "delta": round(overall_after - overall_before, 1),
    }


def _generate_recommendations(
    phase1: dict, phase2: dict, patterns: dict, course_probe: dict
) -> list[dict]:
    """Correlate Phase 1 issues with Phase 2 findings to produce actionable fix suggestions."""

    if phase1.get("status") != "ok":
        return []

    fc = phase1.get("field_completion", {})
    detected = phase2.get("detected", {}) if phase2.get("status") == "ok" else {}
    total = max(phase1.get("courses_analysed", 1), 1)
    recs: list[dict] = []

    def _pct(field: str) -> float:
        return fc.get(field, {}).get("pct", 1.0)

    def _missing(field: str) -> int:
        return fc.get(field, {}).get("missing", 0)

    # Helpers to pull evidence from course_probe
    def _first_ielts_page() -> dict:
        for p in course_probe.get("per_page", []):
            if p.get("is_ielts_url"):
                return p
        return {}

    def _first_fee_page() -> dict:
        for p in course_probe.get("per_page", []):
            if p.get("is_fee_url"):
                return p
        return {}

    # ── 1. Missing IELTS ───────────────────────────────────────────────────────
    if _pct("ielts_overall") < 0.5 and _missing("ielts_overall") > 0:
        english_links = detected.get("english_link_texts", [])
        ielts_page = _first_ielts_page()
        page_signals = ielts_page.get("signals", {})
        page_snippets = ielts_page.get("detected_snippets", [])

        # Build evidence block
        evidence: dict[str, Any] = {
            "affected_count": _missing("ielts_overall"),
            "sample_url": ielts_page.get("url"),
            "detected_snippets": page_snippets[:3],
            "page_signals": {k: v for k, v in page_signals.items() if v},
        }

        if english_links:
            best = _best_link_texts(english_links, [
                "english language requirements",
                "english requirements",
                "language requirements",
                "admissions policy",
                "ielts",
            ])
            # Decide recipe: band mapping if band text on page, else follow-links
            recipe_patch: dict[str, Any] = {"english.follow_links": best}
            if page_signals.get("band_text"):
                recipe_patch["english.band_mapping"] = {}
            desc = f'Add English follow-links: {", ".join(repr(t) for t in best[:3])}'
            if page_signals.get("band_text"):
                desc += ". Band text detected on page — also configure Band Mapping."
            recs.append({
                "severity": "critical",
                "id": "missing_ielts_follow_link",
                "title": f"{_missing('ielts_overall')} courses missing IELTS",
                "description": (
                    f"Only {int(_pct('ielts_overall') * 100)}% of courses have English requirements. "
                    "English requirement pages were detected on the live site but the scraper is not following them."
                    + (f" Band text was found on sampled course pages — a Band Mapping rule may be needed." if page_signals.get("band_text") else "")
                ),
                "root_cause": "Extraction configuration issue — English page exists but scraper is not following it",
                "confidence": 0.90,
                "impact_estimate": _impact_estimate(phase1, ["ielts_overall"]),
                "evidence": evidence,
                "fix": {
                    "type": "recipe_fix",
                    "description": desc,
                    "recipe_patch": recipe_patch,
                },
            })
        elif course_probe.get("english_section_found") or course_probe.get("band_text_found"):
            recipe_patch = {}
            if course_probe.get("band_text_found"):
                recipe_patch["english.band_mapping"] = {}
                recipe_patch["english.band_reference_url"] = ""
            if course_probe.get("english_section_found"):
                recipe_patch["english.follow_links"] = ["English language requirements"]
            recs.append({
                "severity": "critical",
                "id": "missing_ielts_page_has_data",
                "title": f"{_missing('ielts_overall')} courses missing IELTS",
                "description": (
                    f"Only {int(_pct('ielts_overall') * 100)}% of courses have English requirements. "
                    + ("English section was detected in course page content. " if course_probe.get("english_section_found") else "")
                    + ("Band text (e.g. Band 2) was found — a Band Mapping rule is needed to convert band levels to IELTS scores." if course_probe.get("band_text_found") else "")
                ),
                "root_cause": "Extraction configuration issue — English data exists on page but extraction rule is missing",
                "confidence": 0.85,
                "impact_estimate": _impact_estimate(phase1, ["ielts_overall"]),
                "evidence": evidence,
                "fix": {
                    "type": "recipe_fix",
                    "description": (
                        "Configure Band Mapping in the IELTS & Intake tab, or add an English follow-link."
                        if course_probe.get("band_text_found")
                        else "Add English follow-links in the IELTS & Intake tab."
                    ),
                    "recipe_patch": recipe_patch,
                },
            })
        elif _pct("ielts_overall") < 0.15:
            recs.append({
                "severity": "critical",
                "id": "missing_ielts_no_link",
                "title": f"{_missing('ielts_overall')} courses missing IELTS",
                "description": (
                    f"Only {int(_pct('ielts_overall') * 100)}% of courses have English requirements. "
                    "No English requirements links were found during the live probe. "
                    "Requirements may be in a PDF, behind a band system, or hidden in an accordion."
                ),
                "root_cause": "English requirements not found in standard link positions — may need manual inspection",
                "confidence": 0.60,
                "impact_estimate": _impact_estimate(phase1, ["ielts_overall"]),
                "evidence": evidence,
                "fix": {
                    "type": "recipe_fix",
                    "description": (
                        "Add English follow-links in the IELTS & Intake tab, or configure a Band Reference URL "
                        "if English requirements are published as band levels (e.g. Band 1, Band 2)."
                    ),
                    "recipe_patch": {"english.follow_links": [], "english.band_mapping": {}},
                },
            })

    # ── 2. Missing international fee ───────────────────────────────────────────
    if _pct("international_fee") < 0.5 and _missing("international_fee") > 0:
        fee_links = detected.get("fee_link_texts", [])
        fee_page = _first_fee_page()
        fee_signals = fee_page.get("signals", {})
        fee_evidence: dict[str, Any] = {
            "affected_count": _missing("international_fee"),
            "sample_url": fee_page.get("url"),
            "detected_snippets": fee_page.get("detected_snippets", [])[:3],
            "page_signals": {k: v for k, v in fee_signals.items() if v},
        }

        if fee_links:
            best = _best_link_texts(fee_links, [
                "fees and scholarships",
                "international student fees",
                "international fees",
                "fees for your course",
                "tuition fees",
                "view fees",
            ])
            # Add CSP reject keywords if CSP text detected on page
            recipe: dict[str, Any] = {"fees.follow_links": best}
            if course_probe.get("csp_text_found"):
                recipe["fees.reject_keywords"] = ["Commonwealth Supported", "CSP", "Domestic"]
                recipe["fees.prefer_international"] = True
            recs.append({
                "severity": "critical",
                "id": "missing_fee_follow_link",
                "title": f"{_missing('international_fee')} courses missing international fee",
                "description": (
                    f"Only {int(_pct('international_fee') * 100)}% of courses have an international fee. "
                    "Fee pages were detected on the live site but the scraper is not following them. "
                    "This typically means the international fee is on a linked page, not the course page."
                    + (" Domestic/CSP fee text was also detected — reject keywords are needed." if course_probe.get("csp_text_found") else "")
                ),
                "root_cause": "Extraction configuration issue — fee page detected but scraper is not following it",
                "confidence": 0.90,
                "impact_estimate": _impact_estimate(phase1, ["international_fee"]),
                "evidence": fee_evidence,
                "fix": {
                    "type": "recipe_fix",
                    "description": (
                        f'Add fee follow-links: {", ".join(repr(t) for t in best[:3])}'
                        + (". Also add reject keywords: Commonwealth Supported, CSP, Domestic." if course_probe.get("csp_text_found") else "")
                    ),
                    "recipe_patch": recipe,
                },
            })
        elif course_probe.get("international_fee_text_found") or course_probe.get("fee_text_in_blank_pages"):
            recipe = {"fees.prefer_international": True}
            if course_probe.get("csp_text_found"):
                recipe["fees.reject_keywords"] = ["Commonwealth Supported", "CSP", "Domestic"]
            recs.append({
                "severity": "critical",
                "id": "missing_fee_page_has_text",
                "title": f"{_missing('international_fee')} courses missing international fee",
                "description": (
                    f"Only {int(_pct('international_fee') * 100)}% of courses have an international fee. "
                    "Fee amount text was detected in sampled course pages, but the extractor is not capturing it. "
                    + ("Domestic/CSP fee text was also found — reject keywords are needed to skip the domestic fee." if course_probe.get("csp_text_found") else "")
                ),
                "root_cause": "Extraction configuration issue — fee data is on the page but the extraction rule is not matching it",
                "confidence": 0.85,
                "impact_estimate": _impact_estimate(phase1, ["international_fee"]),
                "evidence": fee_evidence,
                "fix": {
                    "type": "recipe_fix",
                    "description": (
                        "Enable 'Prefer International Fee' and add a CSS/XPath selector for the fee element. "
                        + ("Add reject keywords to skip the domestic/CSP fee rows." if course_probe.get("csp_text_found") else "")
                    ),
                    "recipe_patch": recipe,
                },
            })
        elif detected.get("has_tab_layout"):
            recs.append({
                "severity": "critical",
                "id": "missing_fee_tab",
                "title": f"{_missing('international_fee')} courses missing international fee",
                "description": (
                    f"Only {int(_pct('international_fee') * 100)}% of courses have an international fee. "
                    "The site has a tab-based layout — the international fee may be hidden behind an "
                    "'International' tab that requires a browser click to reveal."
                ),
                "root_cause": "International fee hidden behind a tab interaction",
                "confidence": 0.75,
                "impact_estimate": _impact_estimate(phase1, ["international_fee"]),
                "evidence": fee_evidence,
                "fix": {
                    "type": "recipe_fix",
                    "description": "Enable 'Always use browser' in Discovery settings to reveal tab content, then add a fee follow-link",
                    "recipe_patch": {"fees.prefer_international": True},
                },
            })
        else:
            recs.append({
                "severity": "critical",
                "id": "missing_fee_unknown",
                "title": f"{_missing('international_fee')} courses missing international fee",
                "description": (
                    f"Only {int(_pct('international_fee') * 100)}% of courses have an international fee. "
                    "No fee pages were detected during the live probe. "
                    "Check if fees are on a central fee schedule page and add it as a fee source URL."
                ),
                "root_cause": "Fee source not found — add the fee schedule URL to the Recipe Editor",
                "confidence": 0.50,
                "impact_estimate": _impact_estimate(phase1, ["international_fee"]),
                "evidence": fee_evidence,
                "fix": {
                    "type": "recipe_fix",
                    "description": (
                        "Add the international fee schedule page URL to Fee Source URLs in the Recipe Editor. "
                        "If fees are not published per-course, they are usually on a central 'Fees and Scholarships' page."
                    ),
                    "recipe_patch": {"fees.source_urls": []},
                },
            })

    # ── 3. Suspiciously low fee (possible domestic fee stored as international) ─
    low_fee = phase1.get("suspiciously_low_fee_count", 0)
    if low_fee > 3 and _pct("international_fee") > 0.3:
        fee_page = _first_fee_page()
        low_fee_evidence: dict[str, Any] = {
            "affected_count": low_fee,
            "sample_url": fee_page.get("url"),
            "detected_snippets": fee_page.get("detected_snippets", [])[:3],
            "page_signals": {k: v for k, v in fee_page.get("signals", {}).items() if v},
        }
        recs.append({
            "severity": "warning",
            "id": "suspiciously_low_fee",
            "title": f"{low_fee} courses have domestic fee stored as international fee",
            "description": (
                f"{low_fee} courses have an international fee below $10,000, which is unusually low. "
                "The scraper extracted a domestic / CSP fee amount instead of the international one. "
                + ("Domestic fee text (Commonwealth Supported / CSP) was confirmed on sampled pages." if course_probe.get("csp_text_found") else "")
            ),
            "root_cause": "Extraction configuration issue — domestic fee (CSP / Commonwealth Supported) extracted instead of international tuition",
            "confidence": 0.85,
            "impact_estimate": _impact_estimate(phase1, ["international_fee"],
                                                 estimated_fill_pct=0.95,
                                                 courses_affected_override=low_fee),
            "evidence": low_fee_evidence,
            "fix": {
                "type": "recipe_fix",
                "description": 'Add reject keywords: "Commonwealth Supported", "CSP", "Domestic" and enable Prefer International Fee',
                "recipe_patch": {
                    "fees.reject_keywords": ["Commonwealth Supported", "CSP", "Domestic"],
                    "fees.prefer_international": True,
                },
            },
        })

    # ── 4. Study mode may be wrong (mix of Online + On Campus, no Blended) ─────
    sm = phase1.get("study_mode_breakdown", {})
    if (sm.get("Online", 0) > 0 and sm.get("On Campus", 0) > 0
            and sm.get("Blended", 0) == 0
            and detected.get("has_online_delivery")):
        recs.append({
            "severity": "warning",
            "id": "study_mode_blended",
            "title": (
                f"Study mode may be wrong — "
                f"{sm['Online']} Online + {sm['On Campus']} On Campus, 0 Blended"
            ),
            "description": (
                "The scraper found courses classified as both 'Online' and 'On Campus', but none as 'Blended'. "
                "The live site shows online and on-campus delivery side-by-side for the same courses, "
                "which typically means 'Blended' is the correct classification."
            ),
            "root_cause": (
                "The study-mode extractor fires 'Online' at low confidence when it sees online intake dates, "
                "then the location extractor overrides it to 'On Campus'. "
                "The correct value is Blended."
            ),
            "confidence": 0.80,
            "fix": {
                "type": "prefer_blended_over_on_campus",
                "description": "Enable 'Blended' when both online and on-campus signals are present",
                "recipe_patch": None,
            },
        })

    # ── 5. Zero / very low course count ─────────────────────────────────────────
    total_found = phase1.get("total_found", 0)
    imported_count = phase1.get("imported", 0)

    # ── 5a. Wrong pages — links found but 0 courses staged ───────────────────────
    # total_found > 0 means BFS found links; imported == 0 means zero courses were
    # successfully staged into scraped_courses — every discovered page was rejected
    # (degree-qualifier guard, quality gate, or extraction error).
    if total_found > 0 and imported_count == 0:
        recs.insert(0, {
            "severity": "critical",
            "id": "wrong_pages_selected",
            "title": f"{total_found} URL(s) found but all are category listing pages — 0 courses staged",
            "description": (
                f"The scraper discovered {total_found} URL(s) but extracted zero courses from them. "
                "Every page was rejected because its URL slug or page title contains no degree "
                "qualifier (Bachelor, Master, Diploma, etc.) — they are category/subject-area listing "
                "pages, not individual course detail pages. "
                "This is a discovery configuration problem: the correct individual course URL pattern "
                "must be configured before any course data can be extracted."
            ),
            "root_cause": (
                "No 'allow_url_patterns' filter is set, so all BFS-discovered links pass through — "
                "including category hubs like /study/subjects/ and /study/options/. "
                "Alternatively the listing pages render course links via JavaScript so static BFS "
                "never reaches the individual course pages (which ARE accessible as static HTML). "
                "Solution: run Auto-Configure so Gemini probes the live site, detects the CMS "
                "platform, and writes the correct URL depth pattern and browser-discovery settings."
            ),
            "confidence": 0.97,
            "fix": {
                "type": "auto_configure",
                "description": (
                    "Click 'Run Auto-Configure' below to let Gemini analyse the live site and "
                    "generate the correct 'allow_url_patterns' and (if needed) "
                    "'always_browser_discover: true' settings. "
                    "Auto-Configure probes the site, detects the CMS platform, fingerprints "
                    "the URL structure, and writes a tested config — far more reliable than "
                    "manually guessing URL patterns. After Auto-Configure completes, re-run the scrape."
                ),
                "recipe_patch": None,
            },
        })

    if total_found == 0:
        recs.append({
            "severity": "critical",
            "id": "zero_discovery",
            "title": "Zero courses discovered — site requires browser discovery",
            "description": (
                "The scraper found 0 course links. Static BFS crawling returned nothing, "
                "which almost always means the course catalogue is rendered by JavaScript "
                "(React, Vue, Angular, or Squiz Matrix CMS). "
                "No extraction-level recipe change can help until discovery is fixed first."
            ),
            "root_cause": (
                "Extraction configuration issue — the university YAML is missing "
                "'discovery.always_browser_discover: true'. "
                "Enable this setting and add seed_urls pointing to the catalogue pages "
                "so Playwright can follow JavaScript-rendered links."
            ),
            "confidence": 0.95,
            "fix": {
                "type": "config",
                "description": (
                    "Set 'always_browser_discover: true' in the university YAML config. "
                    "Also add seed_urls pointing to the undergraduate, postgraduate, and "
                    "courses listing pages so the browser knows where to start."
                ),
                "recipe_patch": {
                    "discovery.always_browser_discover": "true",
                    "discovery.seed_urls": "<list of course listing page URLs>",
                    "discovery.allow_url_patterns": "<e.g. /study/courses/>",
                },
            },
        })
    elif total_found < 20:
        recs.append({
            "severity": "critical",
            "id": "low_course_count",
            "title": f"Only {total_found} courses discovered",
            "description": (
                f"The last scrape discovered {total_found} courses. "
                "This is unusually low and suggests the discovery phase is incomplete. "
                "Common causes: Cloudflare blocking the scraper, seed URLs pointing to a JavaScript-rendered page, "
                "or URL filters that are too restrictive."
            ),
            "root_cause": (
                "Extraction configuration issue — the seed URLs or URL filters need adjustment, "
                "or the site requires browser-based rendering."
            ),
            "confidence": 0.75,
            "fix": {
                "type": "config",
                "description": "Review seed URLs and URL must-contain filters in the Discovery tab",
                "recipe_patch": {
                    "discovery.always_browser_discover": "true",
                    "discovery.seed_urls": "<list of course listing page URLs>",
                },
            },
        })

    # ── 6. Missing degree level ─────────────────────────────────────────────────
    if _pct("degree_level") < 0.4 and _missing("degree_level") > 5:
        recs.append({
            "severity": "warning",
            "id": "missing_degree_level",
            "title": f"{_missing('degree_level')} courses missing degree level",
            "description": (
                f"Only {int(_pct('degree_level') * 100)}% of courses have a degree level (Bachelor, Master, PhD). "
                "This may affect filtering and categorisation in the portal."
            ),
            "root_cause": (
                "The degree-level extractor looks for keywords in the course title and page heading. "
                "If the university uses non-standard labels (e.g. 'Undergraduate', 'Postgraduate'), "
                "add a field selector to point to the element that contains this information."
            ),
            "confidence": 0.65,
            "impact_estimate": _impact_estimate(phase1, ["degree_level"]),
            "fix": {
                "type": "recipe_fix",
                "description": (
                    "Add a CSS/XPath Field Selector for degree_level in the Recipe Editor → Field Selectors tab, "
                    "targeting the element that shows 'Undergraduate', 'Postgraduate', or equivalent labels."
                ),
                "recipe_patch": {"field_selectors.degree_level": ""},
            },
        })

    # ── 7. Course name pipe suffix ──────────────────────────────────────────────
    pipe_count = patterns.get("pipe_suffix_count", 0)
    sample_pipes = patterns.get("sample_pipe_names", [])
    if pipe_count > 0:
        example = ""
        if sample_pipes:
            suffix = sample_pipes[0].split("|")[-1].strip()[:60]
            example = f' (e.g. "| {suffix}")'
        recs.append({
            "severity": "warning",
            "id": "course_name_pipe_suffix",
            "title": f"{pipe_count} course names contain a university label suffix",
            "description": (
                f"{pipe_count} courses have a pipe character in the course name{example}. "
                "The university name or branding is appended to the degree title and needs to be stripped."
            ),
            "root_cause": (
                "The course title element captures both the degree name and the university label "
                "separated by '|'. The scraper is extracting the full element text."
            ),
            "confidence": 0.90,
            "impact_estimate": {
                "courses_affected": pipe_count,
                "overall_completeness_before": None,
                "overall_completeness_after": None,
                "delta": None,
                "note": "Fixes course name quality — no completeness change",
            },
            "fix": {
                "type": "recipe_fix",
                "description": (
                    "Add a strip suffix rule in the Recipe Editor → Course Name Cleanup: "
                    "'Remove everything after |'. This strips the university brand label."
                ),
                "recipe_patch": {
                    "cleanup.course_name.remove_after": ["|"],
                },
            },
        })

    # ── 8. Band mapping configured but IELTS still blank ───────────────────────
    band_configured = patterns.get("band_mapping_configured", False)
    band_text_found = course_probe.get("band_text_found", False)
    if _pct("ielts_overall") < 0.5 and band_configured:
        if band_text_found:
            recs.append({
                "severity": "critical",
                "id": "band_mapping_not_applied",
                "title": f"{_missing('ielts_overall')} courses — band mapping configured but not applied",
                "description": (
                    f"Band mapping is configured but {_missing('ielts_overall')} courses have no IELTS. "
                    "Band text (e.g. 'Band 2') was detected in sample course pages. "
                    "The band mapping extractor is not matching or the band reference URL is not fetched."
                ),
                "root_cause": (
                    "Band text exists in course pages but the extractor isn't mapping it to IELTS scores. "
                    "The band reference URL or band key names may not match what the site publishes."
                ),
                "confidence": 0.85,
                "impact_estimate": _impact_estimate(phase1, ["ielts_overall"]),
                "fix": {
                    "type": "recipe_fix",
                    "description": (
                        "Verify the Band Reference URL in the IELTS & Intake tab points to the page "
                        "that lists 'Band 1', 'Band 2', etc. with their IELTS equivalents. "
                        "Check that band key names in the recipe exactly match labels on that page."
                    ),
                    "recipe_patch": {"english.band_reference_url": ""},
                },
            })
        elif _pct("ielts_overall") < 0.15:
            recs.append({
                "severity": "critical",
                "id": "band_mapping_ielts_blank",
                "title": f"{_missing('ielts_overall')} courses — band mapping may need tuning",
                "description": (
                    f"Band mapping is configured but {_missing('ielts_overall')} courses have no IELTS. "
                    "Course pages were not accessible (possibly Cloudflare-blocked). "
                    "The band reference URL or key names may not match the live site."
                ),
                "root_cause": "Extraction configuration issue — band mapping configured but band keys may not match site labels",
                "confidence": 0.70,
                "impact_estimate": _impact_estimate(phase1, ["ielts_overall"]),
                "fix": {
                    "type": "recipe_fix",
                    "description": (
                        "Verify the Band Reference URL and that band keys in the recipe "
                        "(e.g. 'Band 2') exactly match the labels on that page"
                    ),
                    "recipe_patch": {"english.band_reference_url": "", "english.band_mapping": {}},
                },
            })

    # ── 9. Fee amount visible in page text but fee is blank ────────────────────
    fee_text_in_blank = course_probe.get("fee_text_in_blank_pages", False)
    if _pct("international_fee") < 0.5 and fee_text_in_blank:
        fee_vis_page = _first_fee_page()
        fee_vis_evidence: dict[str, Any] = {
            "affected_count": _missing("international_fee"),
            "sample_url": fee_vis_page.get("url"),
            "detected_snippets": fee_vis_page.get("detected_snippets", [])[:3],
            "page_signals": {k: v for k, v in fee_vis_page.get("signals", {}).items() if v},
        }
        recs.append({
            "severity": "critical",
            "id": "fee_visible_not_extracted",
            "title": f"{_missing('international_fee')} courses — fee visible in page text but not extracted",
            "description": (
                f"Sample course pages where the fee is blank contain fee amount patterns "
                f"(e.g. '$32,000 per year') in the page text. The fee extractor is not matching them."
            ),
            "root_cause": "Extraction configuration issue — fee data is on the page but extraction pattern is not matching",
            "confidence": 0.85,
            "impact_estimate": _impact_estimate(phase1, ["international_fee"]),
            "evidence": fee_vis_evidence,
            "fix": {
                "type": "recipe_fix",
                "description": (
                    "Add a fee follow-link to the Fees & Scholarships page, or enable Prefer International Fee. "
                    "If fees are in a non-standard table, add a CSS/XPath selector for the fee element."
                ),
                "recipe_patch": {"fees.prefer_international": True},
            },
        })

    # ── 10. CSP / domestic fee text found in course pages ─────────────────────
    csp_found = course_probe.get("csp_text_found", False)
    existing_low_fee = phase1.get("suspiciously_low_fee_count", 0)
    already_has_low_fee_rec = any(r["id"] == "suspiciously_low_fee" for r in recs)
    if csp_found and existing_low_fee > 0 and not already_has_low_fee_rec:
        fee_page_csp = _first_fee_page()
        csp_evidence: dict[str, Any] = {
            "affected_count": existing_low_fee,
            "sample_url": fee_page_csp.get("url"),
            "detected_snippets": fee_page_csp.get("detected_snippets", [])[:3],
            "page_signals": {k: v for k, v in fee_page_csp.get("signals", {}).items() if v},
        }
        recs.append({
            "severity": "critical",
            "id": "csp_domestic_fee_detected",
            "title": f"Domestic fee text detected — {existing_low_fee} courses may have wrong fee",
            "description": (
                f"Course pages contain domestic fee terms ('Commonwealth Supported', 'CSP', 'HECS-HELP'). "
                f"{existing_low_fee} courses have an unusually low fee (< $10,000), "
                "suggesting a domestic fee was stored as the international fee."
            ),
            "root_cause": "Extraction configuration issue — domestic CSP fee extracted instead of international tuition",
            "confidence": 0.90,
            "impact_estimate": _impact_estimate(phase1, ["international_fee"],
                                                 estimated_fill_pct=0.95,
                                                 courses_affected_override=existing_low_fee),
            "evidence": csp_evidence,
            "fix": {
                "type": "recipe_fix",
                "description": "Reject domestic fee keywords and enable Prefer International Fee in the Recipe Editor",
                "recipe_patch": {
                    "fees.reject_keywords": ["Commonwealth Supported", "CSP", "HECS", "Domestic", "Local"],
                    "fees.prefer_international": True,
                },
            },
        })

    # ── 11. Garbage location values ────────────────────────────────────────────
    garbage_loc = patterns.get("garbage_location_count", 0)
    sample_garbage = patterns.get("sample_garbage_locations", [])
    if garbage_loc > 0:
        sample_text = ", ".join(f'"{s}"' for s in sample_garbage[:3])
        campus_allowlist = detected.get("campus_names", [])
        recs.append({
            "severity": "warning",
            "id": "garbage_location",
            "title": f"{garbage_loc} courses have invalid location values",
            "description": (
                f"{garbage_loc} location values contain delivery notes or non-campus text "
                f"({sample_text}). These should be filtered to only show valid campus names."
            ),
            "root_cause": "Extraction configuration issue — location extractor is capturing surrounding text alongside campus names",
            "confidence": 0.88,
            "impact_estimate": {
                "courses_affected": garbage_loc,
                "overall_completeness_before": None,
                "overall_completeness_after": None,
                "delta": None,
                "note": "Fixes location quality — no completeness change",
            },
            "fix": {
                "type": "recipe_fix",
                "description": (
                    "Add known campus names to the Campus Allowlist in the Location tab. "
                    "Add reject keywords for the invalid values seen above."
                ),
                "recipe_patch": {
                    "location.allowed_values": campus_allowlist if campus_allowlist else [],
                    "location.reject_values": ["Not Available", "Not", "TBA", "TBD", "Mixed"],
                },
            },
        })

    # ── 12. IELTS overall exists but component scores blank ────────────────────
    ielts_overall_count = fc.get("ielts_overall", {}).get("count", 0)
    if ielts_overall_count > 5 and course_probe.get("ielts_components_text_found"):
        ielts_page_comp = _first_ielts_page()
        comp_snippets = [s for s in ielts_page_comp.get("detected_snippets", []) if "ielts_components" in s]
        recs.append({
            "severity": "warning",
            "id": "ielts_components_missing",
            "title": f"IELTS overall extracted but component scores missing",
            "description": (
                f"{ielts_overall_count} courses have an IELTS overall score, "
                "but listening/reading/writing/speaking component scores are blank. "
                "Component score text was detected on sampled course pages."
            ),
            "root_cause": "Extraction configuration issue — IELTS components exist on page but component mapping rule is missing",
            "confidence": 0.85,
            "impact_estimate": {
                "courses_affected": ielts_overall_count,
                "overall_completeness_before": None,
                "overall_completeness_after": None,
                "delta": None,
                "note": "Adds component scores — improves data quality, no completeness change",
            },
            "evidence": {
                "affected_count": ielts_overall_count,
                "sample_url": ielts_page_comp.get("url"),
                "detected_snippets": comp_snippets[:3],
                "page_signals": {"ielts_components_text": True},
            },
            "fix": {
                "type": "recipe_fix",
                "description": "Configure IELTS Component Mapping in the IELTS & Intake tab to extract listening/reading/writing/speaking scores",
                "recipe_patch": {
                    "english.component_mapping": {
                        "Listening": 0,
                        "Reading": 0,
                        "Writing": 0,
                        "Speaking": 0,
                    }
                },
            },
        })

    # Sort: critical first, then by confidence (descending)
    recs.sort(key=lambda r: (0 if r["severity"] == "critical" else 1, -r.get("confidence", 0)))

    # Enrich each rec with confidence_reason (human-readable explanation for the score)
    for r in recs:
        if "confidence_reason" not in r:
            r["confidence_reason"] = _confidence_reason(
                confidence=r.get("confidence", 0.5),
                evidence=r.get("evidence") or {},
                affected_count=int((r.get("evidence") or {}).get("affected_count") or 0),
                total=total,
            )

    return recs


def _best_link_texts(detected: list[str], preferred_phrases: list[str]) -> list[str]:
    """Return detected link texts ranked by match to preferred phrases, capped at 5."""
    result: list[str] = []
    detected_lower = [(t.lower(), t) for t in detected]

    for phrase in preferred_phrases:
        for low, orig in detected_lower:
            if phrase in low and orig not in result:
                result.append(orig)

    # Add any remaining detected texts not yet included (up to 5 total)
    for _, orig in detected_lower:
        if orig not in result:
            result.append(orig)

    return result[:5]


# ── Public entry point ─────────────────────────────────────────────────────────

async def run_diagnostics(uni_id: int, db: AsyncSession) -> dict[str, Any]:
    """Run the full diagnostic pipeline and return a structured report.

    Phase 1  — DB analysis of the last completed scrape job.
    Phase 1b — Course-level pattern analysis (name pollution, location garbage, band gaps).
    Phase 2  — Live httpx probe of seed URLs.
    Phase 2b — Sample course-page probe (band text, fee text, CSP text).
    Phase 3  — Cross-correlate everything → actionable recommendations.
    """
    log.info("[DIAGNOSE] Starting diagnostics for uni_id=%s", uni_id)

    phase1 = await _analyse_last_job(uni_id, db)
    log.info("[DIAGNOSE] Phase 1 done: status=%s", phase1.get("status"))

    # Phase 1b — only runs when we have a completed job
    patterns: dict[str, Any] = {
        "uni_name": "",
        "band_mapping_configured": False,
        "pipe_suffix_count": 0,
        "sample_pipe_names": [],
        "garbage_location_count": 0,
        "sample_garbage_locations": [],
        "low_fee_count": 0,
        "blank_ielts_urls": [],
        "blank_fee_urls": [],
    }
    if phase1.get("status") == "ok":
        patterns = await _analyse_course_patterns(phase1["job_id"], uni_id, db)
        log.info("[DIAGNOSE] Phase 1b done: pipe=%d garbage_loc=%d band_configured=%s",
                 patterns["pipe_suffix_count"],
                 patterns["garbage_location_count"],
                 patterns["band_mapping_configured"])

    # Phase 2 + 2b run concurrently
    import asyncio as _asyncio
    phase2_task = _asyncio.create_task(_probe_live_site(uni_id, db))
    course_probe_task = _asyncio.create_task(_probe_sample_course_pages(
        patterns.get("blank_ielts_urls", []),
        patterns.get("blank_fee_urls", []),
    ))
    phase2, course_probe = await _asyncio.gather(phase2_task, course_probe_task)
    log.info("[DIAGNOSE] Phase 2 done: status=%s urls=%s | course_probe: band=%s fee=%s csp=%s",
             phase2.get("status"), len(phase2.get("urls_probed", [])),
             course_probe.get("band_text_found"),
             course_probe.get("fee_text_in_blank_pages"),
             course_probe.get("csp_text_found"))

    recommendations = _generate_recommendations(phase1, phase2, patterns, course_probe)
    log.info("[DIAGNOSE] Phase 3 done: %d recommendations", len(recommendations))

    return {
        "phase1": phase1,
        "phase1b_patterns": patterns,
        "phase2": phase2,
        "phase2b_course_probe": course_probe,
        "recommendations": recommendations,
        "summary": {
            "critical_count": sum(1 for r in recommendations if r["severity"] == "critical"),
            "warning_count": sum(1 for r in recommendations if r["severity"] == "warning"),
            "auto_fix_available": sum(
                1 for r in recommendations if r.get("fix") and r["fix"].get("recipe_patch")
            ),
        },
    }
