#!/usr/bin/env python3
"""Repair already-staged scraped_courses rows:

1. degree_level  — re-derive from course_name for every row where it is NULL
2. course_location — strip "University: Campus" label prefixes and reject
                     bare delivery-mode values ("Mode", "Delivery method", …)

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

from app.services.scraper.extractors.degree_level import classify_degree_level
from app.services.scraper.extractors.location import (
    _INST_LABEL_PREFIX_RE,
    _is_only_delivery_method,
)


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
    if university_id:
        rows = await conn.fetch(
            f"""
            SELECT id, course_name, degree_level
            FROM scraped_courses
            WHERE (degree_level IS NULL OR degree_level = '')
              AND status NOT IN ('rejected', 'approved')
              {uni_filter}
            ORDER BY university_id, id
            """,
            university_id,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT id, course_name, degree_level
            FROM scraped_courses
            WHERE (degree_level IS NULL OR degree_level = '')
              AND status NOT IN ('rejected', 'approved')
            ORDER BY university_id, id
            """
        )

    print(f"Found {len(rows)} rows with missing degree_level")

    dl_updates: list[tuple[str, int]] = []
    for row in rows:
        course_name = (row["course_name"] or "").strip()
        if not course_name:
            continue
        inferred, method, _ = classify_degree_level(course_name)
        if inferred:
            dl_updates.append((inferred, row["id"]))
            print(
                f"  [DL] id={row['id']:6d} {course_name!r:60s} → {inferred!r} ({method})"
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
            SELECT id, course_name, international_fee, fee_currency
            FROM scraped_courses
            WHERE international_fee IS NOT NULL
              AND status NOT IN ('rejected', 'approved')
              AND (
                  (fee_currency = 'GBP' AND international_fee < 10000)
                  OR (fee_currency = 'AUD' AND international_fee < 5000)
              )
              {uni_filter}
            ORDER BY university_id, id
            """,
            university_id,
        )
    else:
        fee_rows = await conn.fetch(
            """
            SELECT id, course_name, international_fee, fee_currency
            FROM scraped_courses
            WHERE international_fee IS NOT NULL
              AND status NOT IN ('rejected', 'approved')
              AND (
                  (fee_currency = 'GBP' AND international_fee < 10000)
                  OR (fee_currency = 'AUD' AND international_fee < 5000)
              )
            ORDER BY university_id, id
            """
        )

    print(f"\nFound {len(fee_rows)} rows with likely domestic/sub-floor fees")
    fee_clear_ids: list[int] = []
    for row in fee_rows:
        cname = (row["course_name"] or "")[:45]
        fee = row["international_fee"]
        cur = row["fee_currency"]
        fee_clear_ids.append(row["id"])
        print(f"  [FEE] id={row['id']:6d} {cname!r:47s} {cur} {fee:,.0f} → clear")

    if not dry_run and fee_clear_ids:
        await conn.executemany(
            "UPDATE scraped_courses SET international_fee = NULL, fee_term = NULL, fee_currency = NULL WHERE id = $1",
            [(i,) for i in fee_clear_ids],
        )
        print(f"→ international_fee cleared on {len(fee_clear_ids)} rows")
    elif dry_run:
        print(f"→ (dry-run) would clear international_fee on {len(fee_clear_ids)} rows")

    await conn.close()

    if not dry_run:
        print(
            f"\n✔ Done: {len(dl_updates)} degree_level fixes, "
            f"{len(loc_updates)} location fixes, "
            f"{len(fee_clear_ids)} domestic fee clears"
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
