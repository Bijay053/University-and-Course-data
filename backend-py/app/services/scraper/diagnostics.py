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


# ── Phase 3: Cross-correlate and generate recommendations ─────────────────────

def _generate_recommendations(phase1: dict, phase2: dict) -> list[dict]:
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
    """Run the full three-phase diagnostic and return a structured report."""

    log.info("[DIAGNOSE] Starting diagnostics for uni_id=%s", uni_id)

    phase1 = await _analyse_last_job(uni_id, db)
    log.info("[DIAGNOSE] Phase 1 done: status=%s", phase1.get("status"))

    phase2 = await _probe_live_site(uni_id, db)
    log.info("[DIAGNOSE] Phase 2 done: status=%s, urls_probed=%s",
             phase2.get("status"), len(phase2.get("urls_probed", [])))

    recommendations = _generate_recommendations(phase1, phase2)
    log.info("[DIAGNOSE] Phase 3 done: %d recommendations", len(recommendations))

    return {
        "phase1": phase1,
        "phase2": phase2,
        "recommendations": recommendations,
        "summary": {
            "critical_count": sum(1 for r in recommendations if r["severity"] == "critical"),
            "warning_count": sum(1 for r in recommendations if r["severity"] == "warning"),
            "auto_fix_available": sum(
                1 for r in recommendations if r.get("fix") and r["fix"].get("recipe_patch")
            ),
        },
    }
