#!/usr/bin/env python3
"""Repair already-staged scraped_courses rows:

1. degree_level    — re-derive from course_name for every row where it is NULL
                     OR where the stored value is not in the canonical set
                     (e.g. full course name stored by mistake: "Music Business
                     - BA (Hons)" → re-inferred as "Bachelor's")
2. course_location — strip "University: Campus" label prefixes and reject
                     bare delivery-mode values ("Mode", "Delivery method", …)
3. domestic fees   — clear GBP < £10,000 and AUD < A$5,000 (home/module rates)
4. wrong-currency  — clear non-GBP fees stored for UK universities (.ac.uk /
                     .co.uk / .uk) — CAD/AUD are misdetections of £ amounts
5. garbage location — clear testimonial / person-name / date / boilerplate text
                     that was extracted as location (BCU/ARU pattern)

Run on dev (Replit):
  cd /home/runner/workspace
  PYTHONPATH=backend-py python3 backend-py/scripts/repair_staging_degree_location.py

Run on production:
  cd /root/University-and-Course-data
  PYTHONPATH=backend-py python3 backend-py/scripts/repair_staging_degree_location.py

Optional flags:
  --dry-run        Print what would change without writing to DB
  --university-id  Restrict repair to one university (numeric ID)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.services.scraper.extractors.degree_level import (
    CANONICAL_DEGREE_LEVELS,
    classify_degree_level,
)
from app.services.scraper.extractors.location import (
    _INST_LABEL_PREFIX_RE,
    _is_only_delivery_method,
)

# ── Garbage location patterns (mirrors stage_course.py guard) ────────────
_LOC_GARBAGE_PERSON_JOB = re.compile(
    r"\b(?:student|graduate|producer|presenter|speaker|professor|"
    r"doctor|director|lecturer|coordinator|researcher|alumni|"
    r"phd\s+student|course\s+leader|bbc|radio\s+\d|programme\s+lead"
    r"|course\s+director)\b",
    re.IGNORECASE,
)
# Separate honorific check — \b at end of alternation fails between [A-Z]
# and the next word character, so this must be its own anchored pattern.
_LOC_GARBAGE_HONORIFIC = re.compile(
    r"^(?:dr|mr|ms|mrs|prof)\.?\s+[A-Za-z]",
    re.IGNORECASE,
)
_LOC_GARBAGE_DATE = re.compile(
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    r"|\b(?:january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\s+\d",
    re.IGNORECASE,
)
_LOC_GARBAGE_PHRASES = re.compile(
    r"(?:please\s+note|worried\s+about|course\s+structure|"
    r"eu\s*/\s*international|\bcredits?\s+of\s+"
    r"|\bdissertation\b|one\s+of\s+(?:our|the)|personal\s+statement"
    r"|sound,|1xtra|radio\s+1)",
    re.IGNORECASE,
)


def _is_garbage_location_part(p: str) -> bool:
    """Return True if this location token looks like garbage (non-campus text)."""
    p = p.strip()
    if not p:
        return True
    if len(p) > 80:
        return True
    if _LOC_GARBAGE_DATE.search(p):
        return True
    if _LOC_GARBAGE_PERSON_JOB.search(p):
        return True
    if _LOC_GARBAGE_HONORIFIC.match(p):
        return True
    if _LOC_GARBAGE_PHRASES.search(p):
        return True
    if re.match(r"^(?:one|some|many|all)\s+of\s*$", p, re.IGNORECASE):
        return True
    return False


def _clean_location(raw: str) -> str | None:
    """Apply the same prefix-strip + delivery-mode reject used in stage_course.py."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if _is_only_delivery_method(raw):
        return None
    if _INST_LABEL_PREFIX_RE.search(raw):
        parts = [_INST_LABEL_PREFIX_RE.sub("", p).strip() for p in raw.split(",")]
        parts = [p for p in parts if p and not _is_only_delivery_method(p)]
        raw = ", ".join(parts)
    if not raw or _is_only_delivery_method(raw):
        return None
    # de-dup parts preserving order
    seen: set[str] = set()
    out: list[str] = []
    for p in [x.strip() for x in raw.split(",")]:
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return ", ".join(out) if out else None


async def _run(dry_run: bool, university_id: int | None) -> None:
    try:
        import asyncpg  # type: ignore
    except ImportError:
        print("ERROR: asyncpg not available — install it or use psycopg2 version")
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL", "")
    # asyncpg wants postgresql:// not postgres://
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://"):]
    # asyncpg doesn't support ?sslmode=disable query param syntax — strip it
    db_url = re.sub(r"\?.*$", "", db_url)

    conn = await asyncpg.connect(db_url)

    uni_filter = "AND university_id = $2" if university_id else ""

    # ── 1. degree_level repair ────────────────────────────────────────────────
    # Fetch rows where degree_level is either:
    #   • NULL / empty                 → was never extracted
    #   • A non-canonical value        → full course name saved by mistake
    #     e.g. "Music Business - BA (Hons)" instead of "Bachelor's"
    # Both cases are re-inferred from course_name using the classifier.
    # asyncpg requires a typed parameter — pass canonical values as text[].
    canonical_list = list(CANONICAL_DEGREE_LEVELS)

    if university_id:
        rows = await conn.fetch(
            """
            SELECT id, course_name, degree_level
            FROM scraped_courses
            WHERE (
                degree_level IS NULL
                OR degree_level = ''
                OR degree_level != ALL($1::text[])
            )
              AND status NOT IN ('rejected', 'approved')
              AND university_id = $2
            ORDER BY university_id, id
            """,
            canonical_list,
            university_id,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT id, course_name, degree_level
            FROM scraped_courses
            WHERE (
                degree_level IS NULL
                OR degree_level = ''
                OR degree_level != ALL($1::text[])
            )
              AND status NOT IN ('rejected', 'approved')
            ORDER BY university_id, id
            """,
            canonical_list,
        )

    print(f"Found {len(rows)} rows with missing/non-canonical degree_level")

    dl_updates: list[tuple[str, int]] = []
    for row in rows:
        course_name = (row["course_name"] or "").strip()
        old_dl = row["degree_level"] or ""
        if not course_name:
            continue
        inferred, method, _ = classify_degree_level(course_name)
        if inferred:
            dl_updates.append((inferred, row["id"]))
            label = "[DL NC]" if old_dl else "[DL NULL]"
            print(
                f"  {label} id={row['id']:6d} {course_name!r:55s} "
                f"old={old_dl!r:20s} → {inferred!r} ({method})"
            )

    if not dry_run and dl_updates:
        await conn.executemany(
            "UPDATE scraped_courses SET degree_level = $1 WHERE id = $2",
            dl_updates,
        )
        print(f"→ degree_level updated on {len(dl_updates)} rows")
    elif dry_run:
        print(f"→ (dry-run) would update degree_level on {len(dl_updates)} rows")

    # ── 2. course_location repair ─────────────────────────────────────────────
    if university_id:
        loc_rows = await conn.fetch(
            f"""
            SELECT id, course_name, course_location
            FROM scraped_courses
            WHERE course_location IS NOT NULL
              AND course_location != ''
              AND status NOT IN ('rejected', 'approved')
              {uni_filter}
            ORDER BY university_id, id
            """,
            university_id,
        )
    else:
        loc_rows = await conn.fetch(
            """
            SELECT id, course_name, course_location
            FROM scraped_courses
            WHERE course_location IS NOT NULL
              AND course_location != ''
              AND status NOT IN ('rejected', 'approved')
            ORDER BY university_id, id
            """
        )

    print(f"\nFound {len(loc_rows)} rows with non-null course_location to inspect")

    loc_updates: list[tuple[str | None, int]] = []
    for row in loc_rows:
        raw_loc = row["course_location"] or ""
        cleaned = _clean_location(raw_loc)
        if cleaned != raw_loc:
            loc_updates.append((cleaned, row["id"]))
            cname = (row["course_name"] or "")[:40]
            print(
                f"  [LOC] id={row['id']:6d} {cname!r:42s} "
                f"{raw_loc!r:45s} → {cleaned!r}"
            )

    if not dry_run and loc_updates:
        await conn.executemany(
            "UPDATE scraped_courses SET course_location = $1 WHERE id = $2",
            loc_updates,
        )
        print(f"→ course_location updated on {len(loc_updates)} rows")
    elif dry_run:
        print(f"→ (dry-run) would update course_location on {len(loc_updates)} rows")

    # ── 3. Domestic / sub-floor fee clearing ─────────────────────────────────
    # Clear international_fee on rows where the stored value is almost
    # certainly a domestic fee:
    #   GBP < £10,000  — home/module/CPD fee for a UK university
    #   AUD < A$5,000  — per-unit or CSP rate
    # These match the new _GBP_INTL_MIN guard and extended _CSP_DOMESTIC_CTX
    # added to fee.py so future scrapes won't pick them up; this clears the
    # already-staged bad rows.
    if university_id:
        fee_rows = await conn.fetch(
            f"""
            SELECT id, course_name, international_fee, currency
            FROM scraped_courses
            WHERE international_fee IS NOT NULL
              AND status NOT IN ('rejected', 'approved')
              AND (
                  (currency = 'GBP' AND international_fee < 10000)
                  OR (currency = 'AUD' AND international_fee < 5000)
              )
              {uni_filter}
            ORDER BY university_id, id
            """,
            university_id,
        )
    else:
        fee_rows = await conn.fetch(
            """
            SELECT id, course_name, international_fee, currency
            FROM scraped_courses
            WHERE international_fee IS NOT NULL
              AND status NOT IN ('rejected', 'approved')
              AND (
                  (currency = 'GBP' AND international_fee < 10000)
                  OR (currency = 'AUD' AND international_fee < 5000)
              )
            ORDER BY university_id, id
            """
        )

    print(f"\nFound {len(fee_rows)} rows with likely domestic/sub-floor fees")
    fee_clear_ids: list[int] = []
    for row in fee_rows:
        cname = (row["course_name"] or "")[:45]
        fee = row["international_fee"]
        cur = row["currency"]
        fee_clear_ids.append(row["id"])
        print(f"  [FEE] id={row['id']:6d} {cname!r:47s} {cur} {fee:,.0f} → clear")

    if not dry_run and fee_clear_ids:
        await conn.executemany(
            "UPDATE scraped_courses SET international_fee = NULL, fee_term = NULL, currency = NULL WHERE id = $1",
            [(i,) for i in fee_clear_ids],
        )
        print(f"→ international_fee cleared on {len(fee_clear_ids)} rows")
    elif dry_run:
        print(f"→ (dry-run) would clear international_fee on {len(fee_clear_ids)} rows")

    # ── 4. Wrong-currency fee repair ─────────────────────────────────────────
    # UK universities (.ac.uk / .co.uk) must always have GBP fees.
    # Two failure modes seen in the wild:
    #   • AUD  — old code defaulted to AUD when no explicit currency symbol
    #   • CAD  — the regex r"CA\$|C\$|CAD" matched an unrelated "C" before "$"
    #            in the flattened page text (e.g. "Course$" or "Contact us…$18,645")
    # Both produce plausible-looking amounts that are actually £-denominated.
    # Clear all non-GBP fees for UK universities so a re-scrape with the new
    # Pre-pass 0 fee table extractor can pick up the correct GBP row.
    _uk_condition = (
        "u.scrape_url ILIKE '%.ac.uk%' OR u.scrape_url ILIKE '%.co.uk%'"
        " OR u.scrape_url ILIKE '%://%.uk/%'"
    )
    if university_id:
        wrong_cur_rows = await conn.fetch(
            f"""
            SELECT sc.id, sc.course_name, sc.international_fee, sc.currency,
                   u.scrape_url
            FROM scraped_courses sc
            JOIN universities u ON u.id = sc.university_id
            WHERE sc.international_fee IS NOT NULL
              AND sc.currency != 'GBP'
              AND sc.status NOT IN ('rejected', 'approved')
              AND ({_uk_condition})
              {uni_filter}
            ORDER BY sc.currency, sc.university_id, sc.id
            """,
            university_id,
        )
    else:
        wrong_cur_rows = await conn.fetch(
            f"""
            SELECT sc.id, sc.course_name, sc.international_fee, sc.currency,
                   u.scrape_url
            FROM scraped_courses sc
            JOIN universities u ON u.id = sc.university_id
            WHERE sc.international_fee IS NOT NULL
              AND sc.currency != 'GBP'
              AND sc.status NOT IN ('rejected', 'approved')
              AND ({_uk_condition})
            ORDER BY sc.currency, sc.university_id, sc.id
            """
        )

    print(f"\nFound {len(wrong_cur_rows)} non-GBP-on-UK-university rows to clear")
    wrong_cur_ids: list[int] = []
    for row in wrong_cur_rows:
        cname = (row["course_name"] or "")[:45]
        cur = row["currency"]
        print(f"  [WCUR] id={row['id']:6d} {cname!r:47s} {cur} {row['international_fee']:,.0f} → clear")
        wrong_cur_ids.append(row["id"])

    if not dry_run and wrong_cur_ids:
        await conn.executemany(
            "UPDATE scraped_courses SET international_fee = NULL, fee_term = NULL, currency = NULL WHERE id = $1",
            [(i,) for i in wrong_cur_ids],
        )
        print(f"→ wrong-currency fee cleared on {len(wrong_cur_ids)} rows")
    elif dry_run:
        print(f"→ (dry-run) would clear wrong-currency fee on {len(wrong_cur_ids)} rows")

    # ── 5. Garbage location clearing ─────────────────────────────────────────
    # BCU / ARU and other universities with embedded testimonial / event
    # blocks on their course pages produce garbage course_location values:
    #   "Worried about Personal Statements?"   → boilerplate
    #   "Friday 4 December"                    → date
    #   "Ben Stones, Producer, Station Sound, BBC Radio 1, …"  → person name
    #   "Please note"                          → boilerplate
    #   "Speaker 1"                            → role label
    #   "Clare Maiden - student"               → person name + role
    # Fetch all non-null location rows and apply the same garbage filter that
    # was added to stage_course.py so future scrapes are also protected.
    if university_id:
        garbage_loc_rows = await conn.fetch(
            f"""
            SELECT id, course_name, course_location
            FROM scraped_courses
            WHERE course_location IS NOT NULL
              AND course_location != ''
              AND status NOT IN ('rejected', 'approved')
              {uni_filter}
            ORDER BY university_id, id
            """,
            university_id,
        )
    else:
        garbage_loc_rows = await conn.fetch(
            """
            SELECT id, course_name, course_location
            FROM scraped_courses
            WHERE course_location IS NOT NULL
              AND course_location != ''
              AND status NOT IN ('rejected', 'approved')
            ORDER BY university_id, id
            """
        )

    print(f"\nChecking {len(garbage_loc_rows)} non-null location rows for garbage text")
    garbage_loc_updates: list[tuple[str | None, int]] = []
    for row in garbage_loc_rows:
        raw = (row["course_location"] or "").strip()
        if not raw:
            continue
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        clean_parts = [p for p in parts if not _is_garbage_location_part(p)]
        if len(clean_parts) != len(parts):
            cleaned = ", ".join(clean_parts) if clean_parts else None
            garbage_loc_updates.append((cleaned, row["id"]))
            cname = (row["course_name"] or "")[:40]
            print(
                f"  [GLOC] id={row['id']:6d} {cname!r:42s} "
                f"{raw!r:55s} → {cleaned!r}"
            )

    if not dry_run and garbage_loc_updates:
        await conn.executemany(
            "UPDATE scraped_courses SET course_location = $1 WHERE id = $2",
            garbage_loc_updates,
        )
        print(f"→ garbage location cleared on {len(garbage_loc_updates)} rows")
    elif dry_run:
        print(f"→ (dry-run) would clear garbage location on {len(garbage_loc_updates)} rows")

    await conn.close()

    if not dry_run:
        print(
            f"\n✔ Done: {len(dl_updates)} degree_level fixes, "
            f"{len(loc_updates)} location fixes, "
            f"{len(fee_clear_ids)} domestic fee clears, "
            f"{len(wrong_cur_ids)} wrong-currency clears, "
            f"{len(garbage_loc_updates)} garbage location clears"
        )
    else:
        print(f"\n(dry-run complete — no changes written)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--university-id", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(_run(args.dry_run, args.university_id))


if __name__ == "__main__":
    main()
