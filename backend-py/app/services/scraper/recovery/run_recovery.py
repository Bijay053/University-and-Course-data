"""Recovery orchestrator — run the Agent Recovery post-pass for a scrape run.

Public entry point: :func:`run_recovery_pass`.

Algorithm
---------
1. Query all staged courses for the run (status=pending or review).
2. For each course, call detector.detect_missing_fields().
3. Batch unique domain-level searches (one BFS per university domain).
4. For each candidate page URL, fetch it ONCE and run ALL needed category
   extractors on the same HTML (avoids duplicate fetches when a URL is a
   candidate for multiple categories).
5. Call mapper.map_results_to_course() for each course × field.
6. Write results to agent_recovery_results (idempotent — skip existing rows).
7. Return a summary dict.

A failure in the recovery pass must never fail the main scrape job.  The
caller (orchestrator.run_scrape) wraps the call in try/except.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


async def _get_courses_for_run(
    db: AsyncSession, scrape_run_id: str
) -> list[dict[str, Any]]:
    """Return all pending/review staged courses for the run."""
    from app.models import ScrapedCourse
    rows = (await db.execute(
        select(ScrapedCourse).where(
            ScrapedCourse.scrape_job_id == scrape_run_id,
            ScrapedCourse.status.in_(["pending", "review"]),
        )
    )).scalars().all()

    courses = []
    for r in rows:
        courses.append({
            "id": r.id,
            "university_id": r.university_id,
            "course_name": r.course_name,
            "degree_level": r.degree_level,
            "international_fee": r.international_fee,
            "ielts_overall": r.ielts_overall,
            "intake_months": r.intake_months,
            "course_location": r.course_location,
            "other_requirement": r.other_requirement if hasattr(r, "other_requirement") else None,
            "course_website": r.course_website,
            "status": r.status,
        })
    return courses


async def _get_evidence_for_courses(
    db: AsyncSession, course_ids: list[int]
) -> dict[int, list[dict[str, Any]]]:
    """Bulk-load evidence rows keyed by scraped_course_id."""
    if not course_ids:
        return {}
    rows = (await db.execute(
        text(
            "SELECT scraped_course_id, field_key, confidence "
            "FROM scraped_field_evidence "
            "WHERE scraped_course_id = ANY(:ids)"
        ),
        {"ids": course_ids},
    )).all()
    out: dict[int, list[dict[str, Any]]] = {cid: [] for cid in course_ids}
    for row in rows:
        out[row.scraped_course_id].append({
            "field_key": row.field_key,
            "confidence": row.confidence,
        })
    return out


async def _get_university_info(
    db: AsyncSession, university_id: int
) -> dict[str, Any]:
    """Return scrape_url, country, scrape_config for a university."""
    row = (await db.execute(
        text(
            "SELECT scrape_url, country, scrape_config "
            "FROM universities WHERE id = :uid"
        ),
        {"uid": university_id},
    )).first()
    if not row:
        return {}
    return {
        "scrape_url": row.scrape_url,
        "country": row.country,
        "scrape_config": row.scrape_config or {},
    }


async def _existing_recovery_fields(
    db: AsyncSession, scraped_course_id: int, scrape_run_id: str
) -> set[str]:
    """Return fields that already have pending/applied results for this run."""
    rows = (await db.execute(
        text(
            "SELECT field FROM agent_recovery_results "
            "WHERE scraped_course_id = :sc_id "
            "  AND scrape_run_id = :run_id "
            "  AND status IN ('pending', 'applied')"
        ),
        {"sc_id": scraped_course_id, "run_id": scrape_run_id},
    )).all()
    return {r[0] for r in rows}


async def _write_recovery_results(
    db: AsyncSession,
    scraped_course_id: int,
    scrape_run_id: str,
    mapped: dict[str, dict[str, Any]],
) -> int:
    """Insert recovery results and return count of new rows written."""
    if not mapped:
        return 0
    written = 0
    for field, result in mapped.items():
        value = result.get("value")
        if value is None:
            continue
        # Serialise value to string for storage
        if isinstance(value, (list, dict)):
            import json
            value_str = json.dumps(value)
        else:
            value_str = str(value)

        await db.execute(
            text(
                """
                INSERT INTO agent_recovery_results
                    (scraped_course_id, scrape_run_id, field, recovered_value,
                     source_url, source_type, evidence_text, confidence,
                     mapping_reason, status, created_at)
                VALUES
                    (:sc_id, :run_id, :field, :value,
                     :source_url, :source_type, :evidence_text, :confidence,
                     :mapping_reason, 'pending', NOW())
                """
            ),
            {
                "sc_id": scraped_course_id,
                "run_id": scrape_run_id,
                "field": field,
                "value": value_str,
                "source_url": result.get("source_url"),
                "source_type": result.get("source_type", "html"),
                "evidence_text": result.get("snippet"),
                "confidence": result.get("confidence"),
                "mapping_reason": result.get("mapping_reason"),
            },
        )
        written += 1
    if written:
        await db.commit()
    return written


async def run_recovery_pass(
    scrape_run_id: str,
    db: AsyncSession,
    *,
    emit=None,
) -> dict[str, Any]:
    """Run the Agent Recovery post-pass for a completed scrape run.

    Parameters
    ----------
    scrape_run_id:
        The runtime_job_id of the completed scrape run.
    db:
        SQLAlchemy AsyncSession.
    emit:
        Optional callable(event, message, **kw) for streaming log lines.

    Returns
    -------
    dict with keys: courses_examined, fields_recovered, results_written
    """
    from app.services.scraper.recovery.detector import detect_missing_fields
    from app.services.scraper.recovery.searcher import (
        search_candidate_pages,
        FIELD_TO_CATEGORY,
    )
    from app.services.scraper.recovery.extractor import extract_from_url
    from app.services.scraper.recovery.mapper import map_results_to_course

    log.info("[RECOVERY] starting recovery pass for run %r", scrape_run_id)
    if emit:
        await emit("status", "[RECOVERY] Agent Recovery pass starting…", phase="recovery")

    courses = await _get_courses_for_run(db, scrape_run_id)
    if not courses:
        log.info("[RECOVERY] no staged courses found for run %r", scrape_run_id)
        return {"courses_examined": 0, "fields_recovered": 0, "results_written": 0}

    course_ids = [c["id"] for c in courses]
    evidence_map = await _get_evidence_for_courses(db, course_ids)

    # Group courses by university_id so we only BFS-search each domain once
    uni_groups: dict[int, list[dict[str, Any]]] = {}
    for course in courses:
        uid = course["university_id"]
        uni_groups.setdefault(uid, []).append(course)

    total_examined = 0
    total_fields = 0
    total_written = 0

    for uni_id, uni_courses in uni_groups.items():
        uni_info = await _get_university_info(db, uni_id)
        scrape_url = uni_info.get("scrape_url") or ""
        country = uni_info.get("country")
        scrape_config = uni_info.get("scrape_config") or {}

        if not scrape_url:
            log.info(
                "[RECOVERY] uni_id=%s has no scrape_url — skipping %d courses",
                uni_id, len(uni_courses),
            )
            continue

        # Determine which field categories are needed across all courses in this uni
        needed_fields_union: set[str] = set()
        course_needed_map: dict[int, list[str]] = {}
        for course in uni_courses:
            ev = evidence_map.get(course["id"], [])
            needed = detect_missing_fields(course, ev, scrape_config)
            if needed:
                course_needed_map[course["id"]] = needed
                needed_fields_union.update(needed)
            total_examined += 1

        if not needed_fields_union:
            log.debug(
                "[RECOVERY] uni_id=%s — no fields need recovery across %d courses",
                uni_id, len(uni_courses),
            )
            continue

        needed_categories = {
            FIELD_TO_CATEGORY[f] for f in needed_fields_union
            if f in FIELD_TO_CATEGORY
        }
        log.info(
            "[RECOVERY] uni_id=%s scrape_url=%r — searching for categories=%s",
            uni_id, scrape_url, needed_categories,
        )

        # BFS search — one search per university domain
        try:
            candidates = await search_candidate_pages(scrape_url, needed_categories)
        except Exception as exc:
            log.warning("[RECOVERY] search failed for uni_id=%s: %s", uni_id, exc)
            candidates = []

        if not candidates:
            log.info("[RECOVERY] uni_id=%s — no candidates found", uni_id)
            continue

        # ------------------------------------------------------------------
        # Fix: group candidates by URL so each URL is fetched EXACTLY ONCE.
        # A URL may be a candidate for multiple categories (e.g. an admissions
        # page can contain both IELTS requirements and entry requirements).
        # Running all needed category extractors on the same HTML avoids the
        # duplicate-fetch problem and ensures no category is silently skipped.
        # ------------------------------------------------------------------
        url_to_categories: dict[str, set[str]] = {}
        for cand in candidates:
            url_to_categories.setdefault(cand["url"], set()).add(cand["category"])

        extracted_by_category: dict[str, list[dict[str, Any]]] = {}
        for url, url_cats in url_to_categories.items():
            try:
                page_results = await extract_from_url(url, url_cats, country=country)
                for res in page_results:
                    field = res.get("field", "")
                    res_cat = FIELD_TO_CATEGORY.get(field, "")
                    if res_cat:
                        extracted_by_category.setdefault(res_cat, []).append(res)
            except Exception as exc:
                log.warning(
                    "[RECOVERY] extraction error for url=%r: %s", url, exc
                )

        if not extracted_by_category:
            log.info("[RECOVERY] uni_id=%s — no values extracted", uni_id)
            continue

        # Map results to each course
        for course in uni_courses:
            course_id = course["id"]
            needed = course_needed_map.get(course_id)
            if not needed:
                continue

            # Skip fields already having recovery results for this run
            existing = await _existing_recovery_fields(db, course_id, scrape_run_id)

            all_results: list[dict[str, Any]] = []
            for field in needed:
                if field in existing:
                    log.debug(
                        "[RECOVERY] course=%s field=%r — already has result, skipping",
                        course_id, field,
                    )
                    continue
                cat = FIELD_TO_CATEGORY.get(field)
                if cat:
                    all_results.extend(extracted_by_category.get(cat, []))

            if not all_results:
                continue

            mapped = map_results_to_course(
                all_results,
                degree_level=course.get("degree_level"),
                course_name=course.get("course_name"),
            )

            # Only write fields this course actually needs
            mapped_for_course = {
                f: v for f, v in mapped.items()
                if f in needed and f not in existing
            }

            if mapped_for_course:
                written = await _write_recovery_results(
                    db, course_id, scrape_run_id, mapped_for_course
                )
                total_written += written
                total_fields += len(mapped_for_course)
                log.info(
                    "[RECOVERY] course=%s — wrote %d recovery result(s): %s",
                    course_id, written, list(mapped_for_course.keys()),
                )

    summary = {
        "courses_examined": total_examined,
        "fields_recovered": total_fields,
        "results_written": total_written,
    }
    log.info("[RECOVERY] pass complete for run %r — %s", scrape_run_id, summary)
    if emit:
        await emit(
            "status",
            f"[RECOVERY] Agent Recovery complete — {total_examined} courses examined, "
            f"{total_written} recovery results written",
            phase="recovery",
            **summary,
        )
    return summary


async def run_single_course_recovery(
    scraped_course_id: int,
    db: AsyncSession,
) -> list[dict[str, Any]]:
    """Run a fresh recovery pass for a single staged course.

    Used by the API trigger endpoint.  Deletes existing pending results for
    this course before re-running so operators always see fresh results.

    Returns
    -------
    list of newly-written result dicts (for immediate API response).
    """
    from app.models import ScrapedCourse
    from app.services.scraper.recovery.detector import detect_missing_fields
    from app.services.scraper.recovery.searcher import (
        search_candidate_pages,
        FIELD_TO_CATEGORY,
    )
    from app.services.scraper.recovery.extractor import extract_from_url
    from app.services.scraper.recovery.mapper import map_results_to_course

    sc = await db.get(ScrapedCourse, scraped_course_id)
    if not sc:
        log.warning("[RECOVERY] trigger: course %s not found", scraped_course_id)
        return []

    scrape_run_id = sc.scrape_job_id

    # Load evidence
    ev_rows = (await db.execute(
        text(
            "SELECT field_key, confidence FROM scraped_field_evidence "
            "WHERE scraped_course_id = :sc_id"
        ),
        {"sc_id": scraped_course_id},
    )).all()
    evidence = [{"field_key": r.field_key, "confidence": r.confidence} for r in ev_rows]

    # University info
    uni_info = await _get_university_info(db, sc.university_id)
    scrape_url = uni_info.get("scrape_url") or ""
    country = uni_info.get("country")
    scrape_config = uni_info.get("scrape_config") or {}

    course_dict = {
        "id": sc.id,
        "course_name": sc.course_name,
        "degree_level": sc.degree_level,
        "international_fee": sc.international_fee,
        "ielts_overall": sc.ielts_overall,
        "intake_months": sc.intake_months,
        "course_location": sc.course_location,
        "other_requirement": getattr(sc, "other_requirement", None),
    }

    needed = detect_missing_fields(course_dict, evidence, scrape_config)
    if not needed:
        log.info("[RECOVERY] trigger: course %s — no missing fields", scraped_course_id)
        return []

    if not scrape_url:
        log.warning("[RECOVERY] trigger: course %s has no university scrape_url", scraped_course_id)
        return []

    needed_categories = {FIELD_TO_CATEGORY[f] for f in needed if f in FIELD_TO_CATEGORY}

    # Delete existing pending results for this course so we get fresh results
    await db.execute(
        text(
            "DELETE FROM agent_recovery_results "
            "WHERE scraped_course_id = :sc_id AND status = 'pending'"
        ),
        {"sc_id": scraped_course_id},
    )
    await db.commit()

    candidates = await search_candidate_pages(scrape_url, needed_categories)
    if not candidates:
        return []

    # Group by URL to avoid duplicate fetches when a URL covers multiple categories
    url_to_categories: dict[str, set[str]] = {}
    for cand in candidates:
        url_to_categories.setdefault(cand["url"], set()).add(cand["category"])

    all_results: list[dict[str, Any]] = []
    for url, url_cats in url_to_categories.items():
        try:
            page_results = await extract_from_url(url, url_cats, country=country)
            all_results.extend(page_results)
        except Exception as exc:
            log.warning("[RECOVERY] trigger extract error %r: %s", url, exc)

    mapped = map_results_to_course(
        all_results,
        degree_level=sc.degree_level,
        course_name=sc.course_name,
    )
    mapped_for_course = {f: v for f, v in mapped.items() if f in needed}

    await _write_recovery_results(db, scraped_course_id, scrape_run_id, mapped_for_course)

    # Return fresh results from DB
    rows = (await db.execute(
        text(
            "SELECT id, scraped_course_id, scrape_run_id, field, recovered_value, "
            "source_url, source_type, evidence_text, confidence, mapping_reason, "
            "status, created_at "
            "FROM agent_recovery_results "
            "WHERE scraped_course_id = :sc_id "
            "ORDER BY id DESC"
        ),
        {"sc_id": scraped_course_id},
    )).all()

    return [
        {
            "id": r.id,
            "scrapedCourseId": r.scraped_course_id,
            "scrapeRunId": r.scrape_run_id,
            "field": r.field,
            "recoveredValue": r.recovered_value,
            "sourceUrl": r.source_url,
            "sourceType": r.source_type,
            "evidenceText": r.evidence_text,
            "confidence": r.confidence,
            "mappingReason": r.mapping_reason,
            "status": r.status,
            "createdAt": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
