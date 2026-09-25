"""Post-release ULaw acceptance through the normal staged re-extract API.

Dry-run by default. --apply requires PORTAL_ACCEPTANCE_SESSION (an existing
authenticated session cookie); never prints the cookie. No publishing endpoint
or direct database mutation is used. Run from backend-py with PYTHONPATH=.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import ScrapedCourse
from app.services.scraper.extractors.ulaw_fees import parse_course_fees


async def run(args):
    parsed = urlparse(args.base_url)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit("Acceptance API must be loopback on the target deployment")
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(ScrapedCourse).where(
            ScrapedCourse.university_id == 92,
            ScrapedCourse.scrape_job_id == "job_f91741363ebf",
            ScrapedCourse.status.notin_(["approved", "published", "rejected"]),
        ).order_by(ScrapedCourse.id))).scalars().all()
        snapshots = [{
            "id": row.id, "url": row.course_website,
            "fee": row.international_fee, "term": row.fee_term,
            "year": row.fee_year, "currency": row.currency,
            "variants": (row.extraction_method or {}).get("fee_variants"),
            "untouched": {
                column.name: str(getattr(row, column.name))
                for column in ScrapedCourse.__table__.columns
                if column.name in {
                    "course_name", "course_website", "course_location", "duration",
                    "duration_term", "study_mode", "ielts_overall", "pte_overall",
                    "toefl_overall", "intake_months", "status", "scrape_job_id",
                }
            },
        } for row in rows]
    if not snapshots or len(snapshots) > 85:
        raise SystemExit(f"Safety bound failed: expected 1–85 eligible rows, got {len(snapshots)}")
    token = os.environ.get("PORTAL_ACCEPTANCE_SESSION")
    if args.apply and not token:
        raise SystemExit("--apply requires PORTAL_ACCEPTANCE_SESSION")
    unresolved = []
    affected = []
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as official:
        for row in snapshots:
            # Exactly one official HTML request per row; no incidental PDFs,
            # unrelated central defaults, or unbounded link crawling.
            if urlparse(row["url"] or "").hostname not in {"law.ac.uk", "www.law.ac.uk"}:
                raise SystemExit(f"Unexpected saved course host at row {row['id']}")
            response = await official.get(row["url"])
            response.raise_for_status()
            if urlparse(str(response.url)).hostname not in {"law.ac.uk", "www.law.ac.uk"}:
                raise SystemExit("Official fetch redirected to an unrelated host")
            authority = parse_course_fees(response.text, row["url"])
            if not authority or authority["status"] == "unresolved":
                unresolved.append(row["id"])
                # Missing rows still get normal SmartFix recovery; nonblank
                # values cannot be forcibly repaired without current evidence.
                if row["fee"] is None:
                    affected.append((row, authority))
                continue
            fresh = (authority["international_fee"], authority["fee_term"], authority["fee_year"], authority["currency"])
            old = (row["fee"], row["term"], row["year"], row["currency"])
            if fresh != old or row["variants"] != authority:
                affected.append((row, authority))
    print(json.dumps({
        "apply": args.apply, "eligible": len(snapshots),
        "affected_ids": [row["id"] for row, _ in affected],
        "unresolved_preflight_ids": unresolved,
    }))
    if not args.apply:
        return
    async with httpx.AsyncClient(base_url=args.base_url, timeout=280,
                                 cookies={"session": token}) as api:
        async def update(row, authority):
            async with AsyncSessionLocal() as db:
                current = await db.get(ScrapedCourse, row["id"])
                if (
                    current is None or current.university_id != 92
                    or current.scrape_job_id != "job_f91741363ebf"
                    or current.status in {"approved", "published", "rejected"}
                    or any(str(getattr(current, key)) != value for key, value in row["untouched"].items())
                    or (current.international_fee, current.fee_term, current.fee_year, current.currency)
                    != (row["fee"], row["term"], row["year"], row["currency"])
                    or (current.extraction_method or {}).get("fee_variants") != row["variants"]
                ):
                    raise RuntimeError(f"Staged snapshot changed before acceptance for row {row['id']}")
            body = {"ids": [row["id"]], "universityId": 92, "smart": True,
                    "targetFields": ["international_fee"]}
            if row["fee"] is not None and authority and authority["status"] != "unresolved":
                body.update({
                    "forceFields": ["international_fee"],
                    "forceReasons": {"international_fee": "Current official course international fee cohort contradicts or qualifies the stored scalar; preserve exact campus alternatives"},
                })
            response = await api.post("/api/scrape/staged/re-extract", json=body)
            response.raise_for_status()
            result = response.json()
            if result.get("errors") or not result.get("results"):
                raise RuntimeError(f"Normal API re-extract failed for row {row['id']}")
            async with AsyncSessionLocal() as db:
                stored = await db.get(ScrapedCourse, row["id"])
                if any(str(getattr(stored, key)) != value for key, value in row["untouched"].items()):
                    raise RuntimeError(f"Untargeted field changed for row {row['id']}")
                variants = (stored.extraction_method or {}).get("fee_variants")
                if authority and authority["status"] != "unresolved" and variants != authority:
                    raise RuntimeError(f"Published fee options did not persist for row {row['id']}")
                print(json.dumps({"id": row["id"], "international_fee": stored.international_fee,
                                  "fee_year": stored.fee_year, "fee_term": stored.fee_term,
                                  "fee_variants": variants, "status": stored.status}))
        semaphore = asyncio.Semaphore(args.concurrency)

        async def bounded(item):
            async with semaphore:
                await update(*item)

        outcomes = await asyncio.gather(*(bounded(item) for item in affected), return_exceptions=True)
        failed_ids = [
            row["id"] for (row, _), outcome in zip(affected, outcomes)
            if isinstance(outcome, BaseException)
        ]
        if failed_ids:
            raise SystemExit(f"Acceptance failed for staged IDs: {failed_ids}; successful rows remain staged")
    if unresolved:
        raise SystemExit(f"Refreshed affected staged rows, but {len(unresolved)} preflight rows still require explicit evidence review")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--concurrency", type=int, choices=[1, 2, 3], default=3)
    args = parser.parse_args()
    # One acceptance writer for this exact review job on the target host.
    # The normal API itself remains responsible for its regular row scope.
    import fcntl
    with open("/tmp/ulaw-fee-acceptance-92-job_f91741363ebf.lock", "w") as lock:
        if args.apply:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit("This job already has an acceptance run on this host")
        asyncio.run(run(args))