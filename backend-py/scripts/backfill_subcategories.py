"""Audit and backfill blank course sub-categories with the shared classifier.

The script is dry-run by default and is safe to rerun.  It updates only rows
whose ``sub_category`` is still NULL/blank at write time, so an operator edit
or concurrent canonical value is never overwritten.

Usage:
    cd backend-py
    PYTHONPATH=. python scripts/backfill_subcategories.py
    PYTHONPATH=. python scripts/backfill_subcategories.py --apply
    PYTHONPATH=. python scripts/backfill_subcategories.py --apply --limit 500
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import AsyncSessionLocal
from app.models import Course, ScrapedCourse, ScrapedFieldEvidence
from app.models.sub_category import CourseSubCategory
from app.services.scraper.category import CATEGORIES, infer_course_taxonomy
from app.services.sub_category_matcher import resolve_sub_category


DEFAULT_REPORT = Path("/tmp/subcategory-backfill-report.json")
BACKFILL_SOURCE = "backfill://subcategory-v1"
EVIDENCE_BATCH_SIZE = 250


@dataclass(frozen=True)
class CandidateRow:
    scope: str
    row_id: int
    university_id: int
    course_name: str
    category: str | None
    sub_category: str | None


@dataclass(frozen=True)
class PlannedChange:
    scope: str
    row_id: int
    university_id: int
    course_name: str
    old_category: str | None
    old_sub_category: str | None
    new_category: str
    new_sub_category: str
    method: str = "deterministic_title_taxonomy"


def plan_change(row: CandidateRow) -> PlannedChange | None:
    """Return a no-overwrite taxonomy change when title evidence is enough."""
    has_category = bool(row.category and row.category.strip())
    has_sub_category = bool(row.sub_category and row.sub_category.strip())
    if has_category and has_sub_category:
        return None
    inferred = infer_course_taxonomy(
        row.course_name,
        category=row.category if has_category else None,
        sub_category=row.sub_category,
    )
    new_category = inferred.get("category")
    new_sub = inferred.get("sub_category")
    # Historical imports sometimes stored a subject ("Economics") or provider
    # faculty label ("Business, Marketing and Management") in the parent
    # column. For blank-child rows only, reclassify those noncanonical parents
    # from the title so the result belongs to the controlled taxonomy.
    if new_category not in CATEGORIES:
        inferred = infer_course_taxonomy(
            row.course_name,
            category=None,
            sub_category=None,
        )
        new_category = inferred.get("category")
        new_sub = inferred.get("sub_category")

    # Never rewrite one valid canonical parent to another during this repair.
    # Noncanonical and legacy parent values cannot own a canonical child and
    # are safe to normalize.
    if row.category and row.category.strip() and new_category != row.category.strip():
        if row.category.strip() in CATEGORIES:
            return None
    if not new_category or new_category not in CATEGORIES or not new_sub:
        return None
    return PlannedChange(
        scope=row.scope,
        row_id=row.row_id,
        university_id=row.university_id,
        course_name=row.course_name,
        old_category=row.category,
        old_sub_category=row.sub_category,
        new_category=new_category,
        new_sub_category=new_sub,
    )


def build_plan(rows: Iterable[CandidateRow]) -> tuple[list[PlannedChange], list[CandidateRow]]:
    changes: list[PlannedChange] = []
    unresolved: list[CandidateRow] = []
    for row in rows:
        change = plan_change(row)
        if change:
            changes.append(change)
        else:
            unresolved.append(row)
    return changes, unresolved


async def _load_candidates(db: Any, limit: int | None) -> list[CandidateRow]:
    staged_stmt = (
        select(
            ScrapedCourse.id,
            ScrapedCourse.university_id,
            ScrapedCourse.course_name,
            ScrapedCourse.category,
            ScrapedCourse.sub_category,
        )
        .where(
            ScrapedCourse.status.in_(["pending", "approved"]),
            or_(
                ScrapedCourse.category.is_(None),
                func.btrim(ScrapedCourse.category) == "",
                ScrapedCourse.sub_category.is_(None),
                func.btrim(ScrapedCourse.sub_category) == "",
            ),
        )
        .order_by(ScrapedCourse.id)
    )
    live_stmt = (
        select(
            Course.id,
            Course.university_id,
            Course.name,
            Course.category,
            Course.sub_category,
        )
        .where(
            Course.status == "active",
            or_(
                Course.category.is_(None),
                func.btrim(Course.category) == "",
                Course.sub_category.is_(None),
                func.btrim(Course.sub_category) == "",
            ),
        )
        .order_by(Course.id)
    )
    if limit:
        staged_stmt = staged_stmt.limit(limit)
        live_stmt = live_stmt.limit(limit)

    staged = (await db.execute(staged_stmt)).all()
    live = (await db.execute(live_stmt)).all()
    return [
        *(
            CandidateRow("scraped_courses", row.id, row.university_id, row.course_name, row.category, row.sub_category)
            for row in staged
        ),
        *(
            CandidateRow("courses", row.id, row.university_id, row.name, row.category, row.sub_category)
            for row in live
        ),
    ]


async def _canonicalize_changes(
    db: Any,
    changes: list[PlannedChange],
) -> tuple[list[PlannedChange], list[CandidateRow]]:
    canonical_pairs = set(
        (
            await db.execute(
                select(CourseSubCategory.category, CourseSubCategory.sub_category)
            )
        ).all()
    )
    cache: dict[tuple[str, str], str] = {}
    canonicalized: list[PlannedChange] = []
    rejected: list[CandidateRow] = []
    for change in changes:
        key = (change.new_category, change.new_sub_category)
        if key not in cache:
            resolved = (
                await resolve_sub_category(
                    db,
                    change.new_category,
                    change.new_sub_category,
                    auto_add=True,
                )
                or change.new_sub_category
            )
            cache[key] = resolved
            canonical_pairs.add((change.new_category, resolved))
        canonicalized.append(
            PlannedChange(
                **{
                    **asdict(change),
                    "new_sub_category": cache[key],
                }
            )
        )
    return canonicalized, rejected


async def _apply_grouped_updates(
    db: Any,
    changes: list[PlannedChange],
) -> set[tuple[str, int]]:
    groups: dict[
        tuple[str, str, str, str | None, str | None], list[int]
    ] = defaultdict(list)
    for change in changes:
        expected_category = (
            change.old_category
            if change.old_category and change.old_category.strip()
            else None
        )
        expected_sub_category = (
            change.old_sub_category
            if change.old_sub_category and change.old_sub_category.strip()
            else None
        )
        groups[
            (
                change.scope,
                change.new_category,
                change.new_sub_category,
                expected_category,
                expected_sub_category,
            )
        ].append(change.row_id)

    updated: set[tuple[str, int]] = set()
    for (
        scope,
        category,
        sub_category,
        expected_category,
        expected_sub_category,
    ), ids in groups.items():
        model = ScrapedCourse if scope == "scraped_courses" else Course
        values: dict[str, Any] = {}
        predicates = [model.id.in_(ids)]
        if expected_category is None:
            values["category"] = category
            predicates.append(
                or_(model.category.is_(None), func.btrim(model.category) == "")
            )
        else:
            # Parent stability is mandatory: a reviewer can edit category while
            # this script is planning.  If it changed, skip rather than attach a
            # child inferred for a stale parent.
            predicates.append(model.category == expected_category)
            if category != expected_category:
                values["category"] = category
        if expected_sub_category is None:
            values["sub_category"] = sub_category
            predicates.append(
                or_(model.sub_category.is_(None), func.btrim(model.sub_category) == "")
            )
        else:
            predicates.append(model.sub_category == expected_sub_category)
        result = await db.execute(
            update(model)
            .where(*predicates)
            .values(**values)
            .returning(model.id)
        )
        updated.update((scope, row_id) for row_id in result.scalars())

    staged_changes = [
        c
        for c in changes
        if ("scraped_courses", c.row_id) in updated
    ]
    if staged_changes:
        evidence_rows = [
            {
                "scraped_course_id": change.row_id,
                "field_key": "sub_category",
                "candidate_value": change.new_sub_category,
                "normalized_value": change.new_sub_category,
                "source_url": BACKFILL_SOURCE,
                "page_type": "historical_backfill",
                "extraction_method": "category:deterministic_backfill",
                "raw_text": change.course_name,
                "snippet": f"Inferred from course title: {change.course_name}",
                "confidence": 0.9,
                "validation_status": "valid",
                "decision_status": "accepted",
                "selected": True,
            }
            for change in staged_changes
        ]
        # asyncpg/PostgreSQL cap one prepared statement at 65,535 bind
        # parameters. Each evidence row has 13 values, so keep inserts bounded
        # well below that limit and make large global backfills reliable.
        for start in range(0, len(evidence_rows), EVIDENCE_BATCH_SIZE):
            batch = evidence_rows[start : start + EVIDENCE_BATCH_SIZE]
            await db.execute(
                pg_insert(ScrapedFieldEvidence)
                .values(batch)
            )
    return updated


def _summary(changes: list[PlannedChange], unresolved: list[CandidateRow]) -> dict[str, Any]:
    return {
        "planned_updates": len(changes),
        "unresolved": len(unresolved),
        "by_scope": dict(Counter(c.scope for c in changes)),
        "by_category": dict(Counter(c.new_category for c in changes)),
        "affected_universities": len({c.university_id for c in changes}),
    }


async def run(*, apply: bool, limit: int | None, report_path: Path) -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        # SHARE blocks INSERT/UPDATE writers on the job table for the short
        # apply transaction, closing the check-then-write window where a scrape
        # could start after the active-job check but before the backfill commit.
        if apply:
            # The development schema predates the model's evidence uniqueness
            # constraint, so serialize script instances explicitly. Blank-only
            # row updates then make evidence insertion idempotent without
            # depending on a database-specific constraint name.
            await db.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtext('subcategory_backfill_v1'))"
                )
            )
            await db.execute(text("LOCK TABLE scrape_runtime_jobs IN SHARE MODE"))
        active_jobs = (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM scrape_runtime_jobs "
                    "WHERE status IN ('running', 'queued')"
                )
            )
        ).scalar_one()
        if apply and active_jobs:
            raise RuntimeError(
                f"Refusing backfill while {active_jobs} scrape job(s) are active; "
                "rerun after they finish"
            )

        candidates = await _load_candidates(db, limit)
        changes, unresolved = build_plan(candidates)
        changes, noncanonical = await _canonicalize_changes(db, changes)
        unresolved.extend(noncanonical)

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "apply" if apply else "dry-run",
            "active_scrape_jobs": active_jobs,
            "summary": _summary(changes, unresolved),
            "changes": [asdict(change) for change in changes],
            "unresolved_rows": [asdict(row) for row in unresolved],
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

        if apply:
            updated = await _apply_grouped_updates(db, changes)
            report["summary"]["actual_updates"] = len(updated)
            await db.commit()
        else:
            await db.rollback()
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Persist planned blank-only updates")
    parser.add_argument("--limit", type=int, default=None, help="Maximum rows per table")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    report = asyncio.run(run(apply=args.apply, limit=args.limit, report_path=args.report))
    print(json.dumps(report["summary"], indent=2))
    print(f"Report: {args.report}")
    if not args.apply:
        print("Dry-run only. Re-run with --apply after reviewing the report.")


if __name__ == "__main__":
    main()