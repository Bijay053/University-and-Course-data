"""Bulk Repair Workflow API.

Two endpoints:

GET  /api/bulk-repair/scan   — scan all universities and return fill-rate
                               issues, grouped by issue type.
POST /api/bulk-repair/apply  — trigger repair scrapes and/or cert-status
                               updates for a selected list of universities.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models import University
from app.models.scrape_runtime import ScrapeRuntimeJob

router = APIRouter()

# ── Thresholds ────────────────────────────────────────────────────────────────
IELTS_THRESHOLD      = 0.50   # fill_rate_ielts_overall  < 50 %
FEE_THRESHOLD        = 0.60   # fill_rate_international_fee < 60 %
DISCOVERY_THRESHOLD  = 70     # health score (0-100)     < 70

# ── Scan ─────────────────────────────────────────────────────────────────────

@router.get("/bulk-repair/scan")
async def bulk_repair_scan(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> dict[str, Any]:
    """Return all universities with their latest fill-rate metrics and issue flags.

    Each row includes boolean flags ``ielts_low``, ``fee_low``,
    ``discovery_low`` so the UI can filter by issue type.  Summary
    counts for each issue type are returned separately.
    """
    rows = (
        await db.execute(
            text(
                """
                WITH latest_summary AS (
                    SELECT DISTINCT ON (s.university_id)
                        s.university_id,
                        s.fill_rate_ielts_overall,
                        s.fill_rate_international_fee,
                        s.fill_rate_duration,
                        s.fill_rate_course_location,
                        s.fill_rate_study_mode,
                        s.candidates_staged,
                        s.run_finished_at
                    FROM scrape_run_summary s
                    ORDER BY s.university_id, s.run_finished_at DESC
                ),
                latest_job AS (
                    SELECT DISTINCT ON (rj.university_id)
                        rj.university_id,
                        rj.runtime_job_id,
                        rj.total_found,
                        rj.imported,
                        rj.created_at AS last_scrape_at
                    FROM scrape_runtime_jobs rj
                    ORDER BY rj.university_id, rj.created_at DESC
                )
                SELECT
                    u.id,
                    u.name,
                    u.country,
                    u.scrape_url,
                    COALESCE(u.certification_status, 'draft') AS certification_status,
                    u.last_certified_score,
                    ls.fill_rate_ielts_overall,
                    ls.fill_rate_international_fee,
                    ls.fill_rate_duration,
                    ls.fill_rate_course_location,
                    ls.fill_rate_study_mode,
                    ls.candidates_staged,
                    ls.run_finished_at,
                    lj.runtime_job_id,
                    lj.total_found,
                    lj.imported,
                    lj.last_scrape_at
                FROM universities u
                LEFT JOIN latest_summary ls ON ls.university_id = u.id
                LEFT JOIN latest_job lj ON lj.university_id = u.id
                ORDER BY u.name
                """
            )
        )
    ).mappings().all()

    universities_out: list[dict] = []
    summary = {"ielts_low": 0, "fee_low": 0, "discovery_low": 0, "total": 0}

    for r in rows:
        # Health score (40/30/30 formula: discovery + completeness + stage rate)
        found    = int(r["total_found"] or 0)
        imported = int(r["imported"] or 0)

        if r["fill_rate_ielts_overall"] is not None:
            avg_comp_pct = (
                float(r["fill_rate_ielts_overall"] or 0) * 30
                + float(r["fill_rate_international_fee"] or 0) * 30
                + float(r["fill_rate_duration"] or 0) * 20
                + float(r["fill_rate_course_location"] or 0) * 20
            )
        else:
            avg_comp_pct = 0.0

        score_found = 40 if found >= 10 else 40 * found / 10
        score_comp  = 30 * min(avg_comp_pct / 100.0, 1.0)
        score_stage = 30 * min(imported / max(found, 1), 1.0) if found else 0
        health_score = round(score_found + score_comp + score_stage)

        # Issue flags
        ielts_pct = float(r["fill_rate_ielts_overall"] or 0) if r["fill_rate_ielts_overall"] is not None else None
        fee_pct   = float(r["fill_rate_international_fee"] or 0) if r["fill_rate_international_fee"] is not None else None

        ielts_low      = ielts_pct is not None and ielts_pct < IELTS_THRESHOLD
        fee_low        = fee_pct   is not None and fee_pct   < FEE_THRESHOLD
        discovery_low  = health_score < DISCOVERY_THRESHOLD

        has_any_issue = ielts_low or fee_low or discovery_low
        if has_any_issue:
            summary["total"] += 1
        if ielts_low:
            summary["ielts_low"] += 1
        if fee_low:
            summary["fee_low"] += 1
        if discovery_low:
            summary["discovery_low"] += 1

        universities_out.append({
            "id": r["id"],
            "name": r["name"],
            "country": r["country"],
            "scrape_url": r["scrape_url"],
            "certification_status": r["certification_status"],
            "last_certified_score": r["last_certified_score"],
            "health_score": health_score,
            "ielts_pct": round(ielts_pct * 100, 1) if ielts_pct is not None else None,
            "fee_pct": round(fee_pct * 100, 1) if fee_pct is not None else None,
            "duration_pct": round(float(r["fill_rate_duration"] or 0) * 100, 1) if r["fill_rate_duration"] is not None else None,
            "location_pct": round(float(r["fill_rate_course_location"] or 0) * 100, 1) if r["fill_rate_course_location"] is not None else None,
            "staged_courses": int(r["candidates_staged"] or 0),
            "total_found": found,
            "last_scrape_at": r["last_scrape_at"].isoformat() if r["last_scrape_at"] else None,
            "ielts_low": ielts_low,
            "fee_low": fee_low,
            "discovery_low": discovery_low,
            "has_any_issue": has_any_issue,
        })

    return {
        "summary": summary,
        "thresholds": {
            "ielts": IELTS_THRESHOLD * 100,
            "fee": FEE_THRESHOLD * 100,
            "discovery": DISCOVERY_THRESHOLD,
        },
        "universities": universities_out,
    }


# ── Preview ───────────────────────────────────────────────────────────────────

@router.post("/bulk-repair/preview")
async def bulk_repair_preview(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
    body: Annotated[dict, Body(...)],
) -> dict[str, Any]:
    """Dry-run preview for a bulk repair — returns issue breakdown and risk signals.

    Body::

        { "university_ids": [1, 2, 3] }

    Returns per-university issue flags, repair target counts, risk signals
    (no seed URL, no repair targets), and aggregated summary counts.
    """
    uni_ids: list[int] = body.get("university_ids") or []
    if not uni_ids:
        raise HTTPException(status_code=400, detail="No university IDs provided")

    # Fetch university basics + repair target counts in one query
    rows = (
        await db.execute(
            text(
                """
                SELECT
                    u.id,
                    u.name,
                    u.scrape_url,
                    COALESCE(u.certification_status, 'draft') AS certification_status,
                    COUNT(c.id) FILTER (
                        WHERE c.status = 'active'
                          AND (
                              c.duration IS NULL
                              OR c.course_location IS NULL
                              OR btrim(COALESCE(c.course_location, '')) = ''
                              OR (SELECT COUNT(*) FROM english_requirements er
                                  WHERE er.course_id = c.id) = 0
                          )
                          AND (c.course_website IS NOT NULL AND btrim(c.course_website) <> '')
                    ) AS repair_target_count,
                    COUNT(c.id) FILTER (WHERE c.status = 'active') AS active_course_count
                FROM universities u
                LEFT JOIN courses c ON c.university_id = u.id
                WHERE u.id = ANY(:ids)
                GROUP BY u.id
                ORDER BY u.name
                """
            ),
            {"ids": uni_ids},
        )
    ).mappings().all()

    unis_out: list[dict] = []
    risk_no_url: list[str] = []
    risk_no_targets: list[str] = []
    total_jobs = 0

    for r in rows:
        no_seed_url = not (r["scrape_url"] or "").strip()
        repair_targets = int(r["repair_target_count"] or 0)
        no_repair_targets = repair_targets == 0

        if no_seed_url:
            risk_no_url.append(r["name"])
        if no_repair_targets:
            risk_no_targets.append(r["name"])
        if not no_repair_targets:
            total_jobs += 1  # one job queued per uni that has targets

        unis_out.append({
            "id": r["id"],
            "name": r["name"],
            "no_seed_url": no_seed_url,
            "no_repair_targets": no_repair_targets,
            "repair_target_count": repair_targets,
            "active_course_count": int(r["active_course_count"] or 0),
        })

    return {
        "selected": len(uni_ids),
        "estimated_jobs": total_jobs,
        "universities": unis_out,
        "risks": {
            "no_seed_url": risk_no_url,
            "no_repair_targets": risk_no_targets,
        },
    }


# ── Apply ─────────────────────────────────────────────────────────────────────

@router.post("/bulk-repair/apply")
async def bulk_repair_apply(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
    body: Annotated[dict, Body(...)],
) -> dict[str, Any]:
    """Trigger repair scrapes and/or cert-status updates for selected universities.

    Body::

        {
          "university_ids": [1, 2, 3],
          "repair_scrape": true,
          "mark_testing": true
        }

    ``repair_scrape`` queues a repair job for each university.
    ``mark_testing`` moves the university cert status to "testing".
    At least one of the two must be true.
    """
    uni_ids: list[int] = body.get("university_ids") or []
    do_repair: bool    = bool(body.get("repair_scrape", True))
    do_testing: bool   = bool(body.get("mark_testing", False))

    if not uni_ids:
        raise HTTPException(status_code=400, detail="No university IDs provided")
    if not do_repair and not do_testing:
        raise HTTPException(status_code=400, detail="At least one action (repair_scrape or mark_testing) must be selected")

    results: list[dict] = []

    for uid in uni_ids:
        uni = await db.get(University, uid)
        if uni is None:
            results.append({"id": uid, "ok": False, "error": "University not found"})
            continue

        job_id: str | None = None
        repair_count = 0

        # ── Repair scrape ─────────────────────────────────────────────────
        if do_repair:
            try:
                repair_rows = (
                    await db.execute(
                        text(
                            """
                            SELECT c.id, c.course_website
                            FROM courses c
                            WHERE c.university_id = :uid
                              AND c.status = 'active'
                              AND (
                                c.duration IS NULL
                                OR c.course_location IS NULL
                                OR btrim(COALESCE(c.course_location, '')) = ''
                                OR (SELECT COUNT(*) FROM english_requirements er
                                    WHERE er.course_id = c.id) = 0
                              )
                            """
                        ),
                        {"uid": uid},
                    )
                ).all()

                targets = [
                    {"course_id": int(r[0]), "url": r[1].strip()}
                    for r in repair_rows
                    if (r[1] or "").strip()
                ]

                if targets:
                    job_id = f"repair_{uuid.uuid4().hex[:12]}"
                    job = ScrapeRuntimeJob(
                        runtime_job_id=job_id,
                        university_id=uni.id,
                        university_name=uni.name,
                        url=uni.scrape_url,
                        job_type="repair",
                        status="queued",
                        request_payload={
                            "universityId": uni.id,
                            "universityName": uni.name,
                            "repair_targets": targets,
                            "triggered_by": "bulk_repair",
                        },
                    )
                    db.add(job)
                    repair_count = len(targets)

                    try:
                        from app.tasks.scrape_tasks import repair_university
                        repair_university.delay(job_id)
                    except Exception:
                        pass

            except Exception as exc:
                results.append({"id": uid, "name": uni.name, "ok": False, "error": str(exc)})
                await db.rollback()
                continue

        # ── Mark testing ──────────────────────────────────────────────────
        if do_testing:
            uni.certification_status = "testing"

        await db.commit()

        results.append({
            "id": uid,
            "name": uni.name,
            "ok": True,
            "repair_job_id": job_id,
            "repair_count": repair_count,
            "marked_testing": do_testing,
        })

    succeeded = sum(1 for r in results if r.get("ok"))
    failed    = len(uni_ids) - succeeded
    skipped   = sum(1 for r in results if r.get("ok") and not r.get("repair_job_id"))

    # ── Audit history record ───────────────────────────────────────────────
    try:
        uni_names = [r["name"] for r in results if "name" in r]
        await db.execute(
            text(
                """
                INSERT INTO bulk_repair_history
                    (triggered_by_email, triggered_by_name,
                     issue_types, selected_count, queued_count,
                     skipped_count, failed_count, mark_testing,
                     university_names, result)
                VALUES
                    (:email, :name,
                     :issue_types, :selected, :queued,
                     :skipped, :failed, :mark_testing,
                     :uni_names, :result)
                """
            ),
            {
                "email":       _user.get("email", "unknown"),
                "name":        _user.get("name"),
                "issue_types": [],
                "selected":    len(uni_ids),
                "queued":      succeeded - skipped,
                "skipped":     skipped,
                "failed":      failed,
                "mark_testing": do_testing,
                "uni_names":   uni_names,
                "result":      __import__("json").dumps(results),
            },
        )
        await db.commit()
    except Exception:
        pass  # audit failure must never block the response

    return {
        "total": len(uni_ids),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }


# ── History ───────────────────────────────────────────────────────────────────

@router.get("/bulk-repair/history")
async def bulk_repair_history(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
    limit: int = 50,
) -> dict[str, Any]:
    """Return the bulk repair audit log, most recent first."""
    rows = (
        await db.execute(
            text(
                """
                SELECT id, created_at, triggered_by_email, triggered_by_name,
                       issue_types, selected_count, queued_count, skipped_count,
                       failed_count, mark_testing, university_names, result
                FROM bulk_repair_history
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    ).mappings().all()

    return {
        "history": [
            {
                "id":                  r["id"],
                "created_at":          r["created_at"].isoformat(),
                "triggered_by_email":  r["triggered_by_email"],
                "triggered_by_name":   r["triggered_by_name"],
                "issue_types":         r["issue_types"] or [],
                "selected_count":      r["selected_count"],
                "queued_count":        r["queued_count"],
                "skipped_count":       r["skipped_count"],
                "failed_count":        r["failed_count"],
                "mark_testing":        r["mark_testing"],
                "university_names":    r["university_names"] or [],
                "result":              r["result"],
            }
            for r in rows
        ]
    }
