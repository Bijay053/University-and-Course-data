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
                                               AND international_fee BETWEEN 1 AND 9999) AS suspiciously_low_fee
        FROM scraped_courses
        WHERE scrape_job_id = :jid
    """), {"jid": job_id})).fetchone()

    total = int(cmp[0] or 1)

    def _pct(n: Any) -> float:
        return round(int(n or 0) / total, 3) if total else 0.0

    field_completion = {
        "international_fee": {
            "count": int(cmp[1] or 0),
            "missing": total - int(cmp[1] or 0),
            "pct": _pct(cmp[1]),
        },
        "ielts_overall": {
            "count": int(cmp[2] or 0),
            "missing": total - int(cmp[2] or 0),
            "pct": _pct(cmp[2]),
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
    }

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

async def _probe_sample_course_pages(
    blank_ielts_urls: list[str],
    blank_fee_urls: list[str],
) -> dict[str, Any]:
    """Probe a small sample of course pages to detect patterns in blank-field courses."""

    band_text_found = False
    fee_text_in_blank_pages = False
    csp_text_found = False

    all_urls = list(dict.fromkeys(blank_ielts_urls[:2] + blank_fee_urls[:2]))

    if not all_urls:
        return {
            "band_text_found": False,
            "fee_text_in_blank_pages": False,
            "csp_text_found": False,
            "cloudflare_blocked_courses": False,
        }

    cloudflare_blocked_courses = False

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=10,
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

                text_content = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)[:8000]

                if _BAND_TEXT_RE.search(text_content) and url in blank_ielts_urls:
                    band_text_found = True

                if _FEE_AMOUNT_RE.search(text_content) and url in blank_fee_urls:
                    fee_text_in_blank_pages = True

                if _CSP_TEXT_RE.search(text_content):
                    csp_text_found = True

            except Exception as exc:
                log.debug("Sample course probe failed for %s: %s", url, exc)

    return {
        "band_text_found": band_text_found,
        "fee_text_in_blank_pages": fee_text_in_blank_pages,
        "csp_text_found": csp_text_found,
        "cloudflare_blocked_courses": cloudflare_blocked_courses,
    }


# ── Phase 3: Cross-correlate and generate recommendations ─────────────────────

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

    # ── 1. Missing IELTS ───────────────────────────────────────────────────────
    if _pct("ielts_overall") < 0.5 and _missing("ielts_overall") > 0:
        english_links = detected.get("english_link_texts", [])
        if english_links:
            best = _best_link_texts(english_links, [
                "english language requirements",
                "english requirements",
                "language requirements",
                "admissions policy",
                "ielts",
            ])
            recs.append({
                "severity": "critical",
                "id": "missing_ielts_follow_link",
                "title": f"{_missing('ielts_overall')} courses missing IELTS",
                "description": (
                    f"Only {int(_pct('ielts_overall') * 100)}% of courses have English requirements. "
                    "English requirement pages were detected on the live site but the scraper is not following them."
                ),
                "root_cause": "English requirements page exists but scraper is not following it",
                "confidence": 0.90,
                "fix": {
                    "type": "english_follow_links",
                    "description": f'Add follow-links: {", ".join(repr(t) for t in best[:3])}',
                    "recipe_patch": {"follow_links": best},
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
                "root_cause": "English requirements not found in standard link positions",
                "confidence": 0.60,
                "fix": None,
            })

    # ── 2. Missing international fee ───────────────────────────────────────────
    if _pct("international_fee") < 0.5 and _missing("international_fee") > 0:
        fee_links = detected.get("fee_link_texts", [])
        if fee_links:
            best = _best_link_texts(fee_links, [
                "fees and scholarships",
                "international student fees",
                "international fees",
                "fees for your course",
                "tuition fees",
                "view fees",
            ])
            recs.append({
                "severity": "critical",
                "id": "missing_fee_follow_link",
                "title": f"{_missing('international_fee')} courses missing international fee",
                "description": (
                    f"Only {int(_pct('international_fee') * 100)}% of courses have an international fee. "
                    "Fee pages were detected on the live site but the scraper is not following them. "
                    "This typically means the international fee is on a linked page, not the course page."
                ),
                "root_cause": "International fee page detected but scraper is not following it",
                "confidence": 0.90,
                "fix": {
                    "type": "fee_follow_links",
                    "description": f'Add fee follow-links: {", ".join(repr(t) for t in best[:3])}',
                    "recipe_patch": {"fee_follow_links": best},
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
                "fix": {
                    "type": "browser_action",
                    "description": "Add a Browser Action to click the 'International' tab before extracting fees",
                    "recipe_patch": None,
                },
            })
        else:
            recs.append({
                "severity": "critical",
                "id": "missing_fee_unknown",
                "title": f"{_missing('international_fee')} courses missing international fee",
                "description": (
                    f"Only {int(_pct('international_fee') * 100)}% of courses have an international fee. "
                    "No fee pages were detected during the live probe."
                ),
                "root_cause": "International fee source not identified — may require manual inspection",
                "confidence": 0.50,
                "fix": None,
            })

    # ── 3. Suspiciously low fee (possible domestic fee stored as international) ─
    low_fee = phase1.get("suspiciously_low_fee_count", 0)
    if low_fee > 3 and _pct("international_fee") > 0.3:
        recs.append({
            "severity": "warning",
            "id": "suspiciously_low_fee",
            "title": f"{low_fee} courses may have domestic fee stored as international fee",
            "description": (
                f"{low_fee} courses have an international fee below $10,000, which is unusually low. "
                "The scraper may have extracted a domestic / CSP fee amount instead of the international one."
            ),
            "root_cause": "Domestic fee (CSP / Commonwealth Supported) extracted instead of international tuition",
            "confidence": 0.80,
            "fix": {
                "type": "fee_reject_keywords",
                "description": 'Add reject keywords: "Commonwealth Supported", "CSP", "Domestic"',
                "recipe_patch": {
                    "fee_reject_keywords": ["Commonwealth Supported", "CSP", "Domestic"],
                    "fee_prefer_international": True,
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

    # ── 5. Very low course count ────────────────────────────────────────────────
    total_found = phase1.get("total_found", 0)
    if total_found < 20:
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
                "Discovery may be incomplete — the seed URLs or URL filters need adjustment, "
                "or the site requires browser-based rendering."
            ),
            "confidence": 0.75,
            "fix": {
                "type": "seed_urls",
                "description": "Review seed URLs and URL must-contain filters in the Discovery tab",
                "recipe_patch": None,
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
            "fix": {
                "type": "field_selector",
                "description": "Add a Field Selector for degree_level in the Field Selectors tab",
                "recipe_patch": None,
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
            "fix": {
                "type": "field_selector",
                "description": (
                    "Add a CSS/XPath field selector for course_name that targets only the text "
                    "before the pipe, or configure a strip_suffix rule in Field Selectors"
                ),
                "recipe_patch": None,
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
                "fix": {
                    "type": "band_reference_url",
                    "description": (
                        "Verify the Band Reference URL in the IELTS & Intake tab points to the page "
                        "that lists 'Band 1', 'Band 2', etc. with their IELTS equivalents"
                    ),
                    "recipe_patch": None,
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
                "root_cause": "Band mapping configured but IELTS still blank — verify band keys match site labels",
                "confidence": 0.70,
                "fix": {
                    "type": "band_reference_url",
                    "description": (
                        "Verify the Band Reference URL and that band keys in the recipe "
                        "(e.g. 'Band 2') exactly match the labels on that page"
                    ),
                    "recipe_patch": None,
                },
            })

    # ── 9. Fee amount visible in page text but fee is blank ────────────────────
    fee_text_in_blank = course_probe.get("fee_text_in_blank_pages", False)
    if _pct("international_fee") < 0.5 and fee_text_in_blank:
        recs.append({
            "severity": "critical",
            "id": "fee_visible_not_extracted",
            "title": f"{_missing('international_fee')} courses — fee visible in page text but not extracted",
            "description": (
                f"Sample course pages where the fee is blank contain fee amount patterns "
                f"(e.g. '$32,000 per year') in the page text. The fee extractor is not matching them."
            ),
            "root_cause": (
                "Fee amounts are present in the HTML but the fee extractor patterns are not matching. "
                "Common causes: amounts in a table with unusual headers, or in a JS-rendered element."
            ),
            "confidence": 0.85,
            "fix": {
                "type": "fee_selector",
                "description": (
                    "Add a Field Selector for international_fee (CSS or XPath) targeting the specific "
                    "fee element, or add a fee follow-link if the fee is on a linked page"
                ),
                "recipe_patch": None,
            },
        })

    # ── 10. CSP / domestic fee text found in course pages ─────────────────────
    csp_found = course_probe.get("csp_text_found", False)
    existing_low_fee = phase1.get("suspiciously_low_fee_count", 0)
    already_has_low_fee_rec = any(r["id"] == "suspiciously_low_fee" for r in recs)
    if csp_found and existing_low_fee > 0 and not already_has_low_fee_rec:
        recs.append({
            "severity": "critical",
            "id": "csp_domestic_fee_detected",
            "title": f"Domestic fee text detected — {existing_low_fee} courses may have wrong fee",
            "description": (
                f"Course pages contain domestic fee terms ('Commonwealth Supported', 'CSP', 'HECS-HELP'). "
                f"{existing_low_fee} courses have an unusually low fee (< $10,000), "
                "suggesting a domestic fee was stored as the international fee."
            ),
            "root_cause": (
                "The fee extractor found a domestic / CSP fee before the international fee. "
                "The international fee is typically higher and labelled differently on the page."
            ),
            "confidence": 0.90,
            "fix": {
                "type": "fee_reject_keywords",
                "description": "Reject domestic fee keywords and prefer the higher international fee",
                "recipe_patch": {
                    "fee_reject_keywords": ["Commonwealth Supported", "CSP", "HECS", "Domestic", "Local"],
                    "fee_prefer_international": True,
                },
            },
        })

    # ── 11. Garbage location values ────────────────────────────────────────────
    garbage_loc = patterns.get("garbage_location_count", 0)
    sample_garbage = patterns.get("sample_garbage_locations", [])
    if garbage_loc > 0:
        sample_text = ", ".join(f'"{s}"' for s in sample_garbage[:3])
        recs.append({
            "severity": "warning",
            "id": "garbage_location",
            "title": f"{garbage_loc} courses have invalid location values",
            "description": (
                f"{garbage_loc} location values contain delivery notes or non-campus text "
                f"({sample_text}). These should be filtered to only show valid campus names."
            ),
            "root_cause": (
                "The location extractor is capturing surrounding text alongside campus names "
                "(e.g. 'Brisbane Not Available', 'Townsville Not'). "
                "A campus allowlist discards everything that isn't a known campus name."
            ),
            "confidence": 0.88,
            "fix": {
                "type": "campus_allowlist",
                "description": (
                    "Add known campus names to the Campus tab (valid_campuses). "
                    "Only values matching the allowlist will be kept."
                ),
                "recipe_patch": None,
            },
        })

    # Sort: critical first, then by confidence (descending)
    recs.sort(key=lambda r: (0 if r["severity"] == "critical" else 1, -r.get("confidence", 0)))
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
