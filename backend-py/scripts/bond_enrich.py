"""Bond University direct API enrichment script.

Skips Playwright entirely. For every Bond row in scraped_courses:
  1. Fetches the course page HTML with requests to parse data-program-detail-url
     and data-program-code (both embedded as data-* attrs in static HTML).
  2. Calls /api/program-details/{id}  → duration, intake_months, category
  3. Calls /api/program-fees/{id}/{code} → international_fee (annual = semester × 3)
  4. Fetches /entry_requirements         → ielts_overall + sub-band scores
  5. UPDATEs the scraped_courses row in Postgres and recalculates completeness.

Usage (run from backend-py directory):
    PYTHONPATH=. venv/bin/python3 scripts/bond_enrich.py [--dry-run] [--workers N]
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlparse

import requests as _requests
from sqlalchemy import create_engine, text

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("bond_enrich")

# ── constants ─────────────────────────────────────────────────────────────────
UNIVERSITY_ID = 22
SEMESTERS_PER_YEAR = 3   # Bond: Jan / May / Sep
PREFERRED_FEE_YEAR = "2026"
REQUEST_TIMEOUT = 12      # seconds per HTTP call
MAX_RETRIES = 2

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,*/*",
}

# ── regex helpers ─────────────────────────────────────────────────────────────
_DETAIL_URL_RE = re.compile(
    r'data-program-detail-url=["\']\/api\/program-details\/(\d+)["\']'
)
_PROG_CODE_RE = re.compile(r'data-program-code=["\']([A-Z0-9\-]+)["\']')
_YEAR_RE = re.compile(r"(\d+(?:\.\d+)?)\s+years?", re.IGNORECASE)
_MONTH_DURATION_RE = re.compile(r"(\d+)\s+months?", re.IGNORECASE)
_IELTS_OVERALL_RE = re.compile(
    r"[Oo]verall\s+(?:band\s+)?(?:score\s+)?(\d+(?:\.\d+)?)"
)
_IELTS_SUB_RE = re.compile(
    r"(?:sub\s*score|band|each\s+(?:sub)?skill|each\s+component|minimum(?:\s+band)?)"
    r"\s+(?:(?:less\s+than\s+|not\s+less\s+than\s+|of\s+)?(?:at\s+least\s+)?)"
    r"(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_OFFERING_MONTH_MAP: dict[str, str] = {
    "jan": "January", "feb": "February", "mar": "March",
    "apr": "April",   "may": "May",       "jun": "June",
    "jul": "July",    "aug": "August",    "sep": "September",
    "oct": "October", "nov": "November",  "dec": "December",
}


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _get(url: str, as_json: bool = False) -> Any:
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = _requests.get(url, headers=_HEADERS, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json() if as_json else r.text
        except Exception as exc:
            if attempt == MAX_RETRIES:
                log.debug("FAIL %s → %s", url, exc)
                return None
            time.sleep(1)


# ── per-course enrichment ─────────────────────────────────────────────────────

def enrich_one(row: dict) -> dict:
    """Enrich a single scraped_courses row. Returns a dict of fields to update."""
    sc_id = row["id"]
    url = (row.get("source_url") or row.get("course_url") or "").strip()
    result: dict[str, Any] = {"id": sc_id}

    if not url:
        log.warning("[%d] no URL, skipping", sc_id)
        return result

    # ── 1. Fetch course page and parse data-* attrs ───────────────────────
    html = _get(url)
    if not html:
        log.warning("[%d] %s — HTML fetch failed", sc_id, url)
        return result

    m_id = _DETAIL_URL_RE.search(html)
    m_code = _PROG_CODE_RE.search(html)
    if not m_id:
        log.warning("[%d] %s — no data-program-detail-url found", sc_id, url)
        return result

    numeric_id = m_id.group(1)
    program_code = m_code.group(1) if m_code else None

    # ── 2. Program details API → duration, intakes, category ─────────────
    details = _get(f"https://bond.edu.au/api/program-details/{numeric_id}", as_json=True)
    if details and isinstance(details, dict):
        programs = details.get("programs", [])
        if programs:
            prog = programs[0]

            dur_str = prog.get("duration", "")
            if dur_str:
                m = _YEAR_RE.search(dur_str)
                if m:
                    result["duration"] = float(m.group(1))
                else:
                    m = _MONTH_DURATION_RE.search(dur_str)
                    if m:
                        result["duration"] = round(int(m.group(1)) / 12, 2)

            offerings = prog.get("offerings", [])
            if offerings:
                months, seen = [], set()
                for o in offerings:
                    key = o.get("semester", "")[:3].lower()
                    name = _OFFERING_MONTH_MAP.get(key)
                    if name and name not in seen:
                        months.append(name)
                        seen.add(name)
                if months:
                    result["intake_months"] = months

            study_areas = prog.get("studyAreas", [])
            if study_areas and study_areas[0].get("label"):
                result["category"] = study_areas[0]["label"]

    # ── 3. Fee API → annual international fee ─────────────────────────────
    if program_code:
        fees_data = _get(
            f"https://bond.edu.au/api/program-fees/{numeric_id}/{program_code}",
            as_json=True,
        )
        if fees_data and isinstance(fees_data, dict):
            fees_list = fees_data.get("fees", [])
            if fees_list:
                fee_entry = next(
                    (f for f in fees_list if str(f.get("year", "")) == PREFERRED_FEE_YEAR),
                    fees_list[0],
                )
                intl = fee_entry.get("international", {})
                sem_fee = intl.get("semester")
                if sem_fee and isinstance(sem_fee, (int, float)):
                    result["international_fee"] = float(sem_fee) * SEMESTERS_PER_YEAR
                    result["fee_term"] = "year"

    # ── 4. Entry requirements → IELTS ─────────────────────────────────────
    base = url.rstrip("/")
    er_html = _get(f"{base}/entry_requirements")
    if er_html:
        text = re.sub(r"<[^>]+>", " ", er_html)
        text = re.sub(r"\s+", " ", text)

        m_ov = _IELTS_OVERALL_RE.search(text)
        if m_ov:
            try:
                result["ielts_overall"] = float(m_ov.group(1))
            except ValueError:
                pass

        m_sub = _IELTS_SUB_RE.search(text)
        if m_sub:
            try:
                sub = float(m_sub.group(1))
                for band in ("ielts_writing", "ielts_reading",
                             "ielts_listening", "ielts_speaking"):
                    result[band] = sub
            except ValueError:
                pass

    log.info(
        "[%d] done: fee=%s ielts=%s duration=%s intakes=%s",
        sc_id,
        result.get("international_fee"),
        result.get("ielts_overall"),
        result.get("duration"),
        result.get("intake_months"),
    )
    return result


# ── DB update ─────────────────────────────────────────────────────────────────

def _build_update(fields: dict) -> tuple[str, dict] | None:
    """Build a parameterised UPDATE for the enriched fields."""
    skip = {"id"}
    params = {k: v for k, v in fields.items() if k not in skip}
    if not params:
        return None

    # intake_months stored as JSON array string
    if "intake_months" in params:
        import json
        params["intake_months"] = json.dumps(params["intake_months"])

    set_clause = ", ".join(f"{k} = :{k}" for k in params)
    params["sc_id"] = fields["id"]
    sql = f"""
        UPDATE scraped_courses
        SET {set_clause}
        WHERE id = :sc_id AND university_id = {UNIVERSITY_ID}
    """
    return sql, params


def recalc_completeness(engine, sc_id: int) -> None:
    """Call the existing completeness function if it exists, else skip."""
    try:
        from app.services.scraper.completeness import calculate_completeness
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM scraped_courses WHERE id = :id"), {"id": sc_id}
            ).mappings().one_or_none()
            if row:
                score = calculate_completeness(dict(row))
                conn.execute(
                    text("UPDATE scraped_courses SET completeness = :c WHERE id = :id"),
                    {"c": score, "id": sc_id},
                )
                conn.commit()
    except Exception as exc:
        log.debug("completeness recalc skipped for %d: %s", sc_id, exc)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich Bond scraped_courses via direct API")
    parser.add_argument("--dry-run", action="store_true", help="Fetch data but don't write to DB")
    parser.add_argument("--workers", type=int, default=8, help="Parallel HTTP workers (default 8)")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N rows (0 = all)")
    parser.add_argument("--id", type=int, default=0, help="Process a single scraped_courses.id")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        log.error("DATABASE_URL not set")
        sys.exit(1)

    # DATABASE_URL may use the async asyncpg driver (postgresql+asyncpg://...).
    # Swap to psycopg2 for synchronous use in this script.
    sync_url = (
        db_url
        .replace("postgresql+asyncpg://", "postgresql+psycopg2://")
        .replace("postgresql+asyncpg+ssl://", "postgresql+psycopg2://")
    )
    # Plain postgresql:// is already sync-compatible with psycopg2.
    engine = create_engine(sync_url, pool_pre_ping=True)

    with engine.connect() as conn:
        q = """
            SELECT id,
                   course_website AS source_url,
                   course_name
            FROM scraped_courses
            WHERE university_id = :uid
              AND status IN ('pending', 'review')
        """
        params: dict = {"uid": UNIVERSITY_ID}
        if args.id:
            q += " AND id = :sid"
            params["sid"] = args.id
        q += " ORDER BY id"
        if args.limit:
            q += f" LIMIT {args.limit}"

        rows = conn.execute(text(q), params).mappings().all()

    rows = [dict(r) for r in rows]
    log.info("Found %d Bond rows to enrich (workers=%d, dry_run=%s)",
             len(rows), args.workers, args.dry_run)

    if not rows:
        log.info("Nothing to do.")
        return

    ok = fee_ok = ielts_ok = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich_one, r): r for r in rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                log.error("[%d] exception: %s", row["id"], exc)
                continue

            if result.get("international_fee"):
                fee_ok += 1
            if result.get("ielts_overall"):
                ielts_ok += 1

            if args.dry_run:
                log.info("[DRY-RUN] would update %d: %s", result["id"],
                         {k: v for k, v in result.items() if k != "id"})
                ok += 1
                continue

            built = _build_update(result)
            if not built:
                continue

            sql, params = built
            with engine.connect() as conn:
                conn.execute(text(sql), params)
                conn.commit()

            recalc_completeness(engine, result["id"])
            ok += 1

    elapsed = time.time() - t0
    log.info(
        "Done in %.1fs — updated %d/%d rows  (fee=%d  ielts=%d)",
        elapsed, ok, len(rows), fee_ok, ielts_ok,
    )

    if not args.dry_run:
        log.info("Next step: run scripts/bulk_approve.py --university-id %d", UNIVERSITY_ID)


if __name__ == "__main__":
    main()
