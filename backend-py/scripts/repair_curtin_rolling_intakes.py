"""Remove invalid Rolling labels from Curtin pending review rows only.

Dry-run by default. Run on the deployment host with --apply to repair.
Creates a mode-0600 before-image backup before changing any rows.
"""
import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import ScrapedCourse, University
from app.models.evidence import ScrapedFieldEvidence
from app.services.auto_publish import should_auto_publish
from app.services.scraper.completeness import compute_completeness, decide_eligibility


def clean_intakes(value):
    if not isinstance(value, list):
        return value
    return [v for v in value if str(v).strip().lower() not in {"rolling", "rol", "roi"}] or None


async def main(apply=False):
    async with AsyncSessionLocal() as db:
        uni = await db.get(University, 33)
        assert uni and "curtin" in uni.name.lower(), "University identity mismatch"
        rows = (await db.execute(
            select(ScrapedCourse).where(
                ScrapedCourse.university_id == uni.id,
                ScrapedCourse.status == "pending",
            ).with_for_update()
        )).scalars().all()
        affected = [r for r in rows if clean_intakes(r.intake_months) != r.intake_months]
        print(json.dumps({"pending": len(rows), "affected": len(affected), "apply": apply}))
        if not apply or not affected:
            await db.rollback()
            return
        ids = [r.id for r in affected]
        evidence = (await db.execute(select(ScrapedFieldEvidence).where(
            ScrapedFieldEvidence.scraped_course_id.in_(ids),
            ScrapedFieldEvidence.field_key == "intake_months",
        ))).scalars().all()
        backup = {
            "rows": [
                {c.name: getattr(r, c.name) for c in r.__table__.columns}
                for r in affected
            ],
            "evidence": [
                {c.name: getattr(e, c.name) for c in e.__table__.columns}
                for e in evidence
            ],
        }
        path = Path("/var/tmp") / (
            "curtin-intake-before-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + ".json"
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(backup, f, default=str)
            f.flush()
            os.fsync(f.fileno())
        for row in affected:
            row.intake_months = clean_intakes(row.intake_months)
            comp = compute_completeness(row)
            row.completeness = comp.score
            eligibility = decide_eligibility(row, comp)
            row.eligibility_status = eligibility.status
            row.eligibility_reason = eligibility.reason or None
            decision = should_auto_publish(row)
            row.auto_publish_status = "ready" if decision.auto_publish else "review"
            row.decision_score = decision.score
        # Keep audit evidence, but invalidate candidates containing the removed label.
        invalidated = 0
        for e in evidence:
            candidate = (str(e.candidate_value or "") + " " + str(e.normalized_value or "")).lower()
            if "rolling" in candidate or candidate.strip() in {"rol", "roi"}:
                e.selected = False
                e.validation_status = "invalid"
                e.decision_status = "rejected"
                invalidated += 1
        await db.commit()
        print(json.dumps({"repaired": len(affected), "evidence_invalidated": invalidated, "backup": str(path)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args().apply))