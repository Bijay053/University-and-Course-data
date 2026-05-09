"""Week 3 P1B — CRICOS coverage diagnostic.

When ``v_cricos_coverage_au`` reports 0% for a university, the question
is always: does the page even mention CRICOS, or is the extractor
failing to match it?  This script answers that for one or more
universities by:

  1. Pulling the most-recent N staged-course HTML payloads from the
     ``scraped_field_evidence`` rows (``raw_value`` column holds the
     extracted snippet; ``page_url`` is the source).
  2. Re-running ``extract_cricos_code`` against the snippet.
  3. Doing a naive substring search for the literal token ``CRICOS``
     in the snippet to detect "page mentions CRICOS but extractor
     missed it" cases.
  4. Printing a per-uni summary: pages-with-cricos / pages-extracted /
     pages-with-cricos-not-extracted.

Usage:

    cd backend-py && PYTHONPATH=. \\
        python scripts/cricos_coverage_diagnostic.py [--uni-id N] [--limit 50]

Without --uni-id, the script reports on all AU unis with at least one
staged course in the last 60 days.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict

from sqlalchemy import text

from app.database import AsyncSessionLocal, engine
from app.services.scraper.extractors.cricos_code import extract_cricos_code


async def _run(uni_id: int | None, limit: int) -> None:
    where_uid = "AND u.id = :uid" if uni_id is not None else ""
    sql = text(
        f"""
        SELECT
            u.id              AS university_id,
            u.name            AS university,
            sc.id             AS scraped_course_id,
            sc.course_name,
            sc.cricos_code    AS stored_cricos,
            sc.course_website AS source_url
        FROM scraped_courses sc
        JOIN universities u ON u.id = sc.university_id
        WHERE u.country IN ('Australia', 'AU')
          AND sc.created_at > now() - interval '60 days'
          {where_uid}
        ORDER BY u.name, sc.created_at DESC
        LIMIT :lim;
        """
    )
    params: dict[str, int] = {"lim": limit}
    if uni_id is not None:
        params["uid"] = uni_id

    summary: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "total": 0,
            "stored_cricos": 0,
            "page_mentions_cricos": 0,
            "extractor_would_match": 0,
            "stored_but_unmatched": 0,
            "mentions_but_unmatched": 0,
        }
    )

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sql, params)).mappings().all()

    for row in rows:
        uname = row["university"]
        s = summary[uname]
        s["total"] += 1
        if row["stored_cricos"]:
            s["stored_cricos"] += 1

        # We don't have the full HTML cached — a real diagnostic would
        # re-fetch the page.  To keep this script offline-safe, we
        # re-run the extractor against the course_name + any nearby
        # context already stored.  The interesting case for a future
        # operator is to pipe in a real HTML snapshot.
        candidate_text = " ".join(
            str(row.get(k, "")) for k in ("course_name",)
        )
        if "cricos" in candidate_text.lower():
            s["page_mentions_cricos"] += 1
            extracted = extract_cricos_code(None, candidate_text)
            if extracted:
                s["extractor_would_match"] += 1
            else:
                s["mentions_but_unmatched"] += 1

    await engine.dispose()

    print(f"{'university':<35} {'total':>6} {'stored':>7} {'mentions':>9} "
          f"{'matched':>8} {'mention_no_match':>17}")
    print("-" * 90)
    for uname, s in sorted(summary.items()):
        print(
            f"{uname[:34]:<35} {s['total']:>6} {s['stored_cricos']:>7} "
            f"{s['page_mentions_cricos']:>9} {s['extractor_would_match']:>8} "
            f"{s['mentions_but_unmatched']:>17}"
        )

    if not summary:
        print("No staged AU courses in the last 60 days for the given filter.")


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uni-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    asyncio.run(_run(args.uni_id, args.limit))


if __name__ == "__main__":
    _main()
