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

# Trace-only statuses — written to record WHY recovery found nothing.
# These rows have recovered_value=NULL and should never be applied/rejected.
_TRACE_STATUSES = frozenset({
    "no_source",
    "no_value",
    "level_mismatch",
    "browser_failed",
    "pdf_failed",
})


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


async def _write_trace_row(
    db: AsyncSession,
    scraped_course_id: int,
    scrape_run_id: str | None,
    field: str,
    status: str,
    reason: str,
    *,
    source_url: str | None = None,
    evidence_text: str | None = None,
) -> None:
    """Write a diagnostic trace row explaining why recovery found nothing.

    Trace rows have recovered_value=NULL and status in _TRACE_STATUSES.
    They are shown in the UI as a "Search Trace" section, never as actionable results.
    """
    await db.execute(
        text(
            """
            INSERT INTO agent_recovery_results
                (scraped_course_id, scrape_run_id, field, recovered_value,
                 source_url, source_type, evidence_text, confidence,
                 mapping_reason, status, created_at)
            VALUES
                (:sc_id, :run_id, :field, NULL,
                 :source_url, 'trace', :evidence_text, NULL,
                 :reason, :status, NOW())
            """
        ),
        {
            "sc_id": scraped_course_id,
            "run_id": scrape_run_id,
            "field": field,
            "source_url": source_url,
            "evidence_text": evidence_text,
            "reason": reason,
            "status": status,
        },
    )


async def _fetch_all_rows_for_course(
    db: AsyncSession, scraped_course_id: int
) -> list[dict[str, Any]]:
    """Return all agent_recovery_results rows for a course, newest first."""
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
    dict with keys: courses_examined, fields_recovered, results_written,
                    pdfs_via_broad_scorer
    """
    from app.services.scraper.recovery.detector import detect_missing_fields
    from app.services.scraper.recovery.searcher import (
        search_candidate_pages,
        FIELD_TO_CATEGORY,
    )
    from app.services.scraper.recovery.extractor import (
        extract_from_url,
        make_pdf_budget,
    )
    from app.services.scraper.recovery.mapper import map_results_to_course

    log.info("[RECOVERY] starting recovery pass for run %r", scrape_run_id)
    if emit:
        await emit("status", "[RECOVERY] Agent Recovery pass starting…", phase="recovery")

    courses = await _get_courses_for_run(db, scrape_run_id)
    if not courses:
        log.info("[RECOVERY] no staged courses found for run %r", scrape_run_id)
        return {"courses_examined": 0, "fields_recovered": 0, "results_written": 0, "pdfs_via_broad_scorer": 0}

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
    total_broad_scorer_pdfs = 0

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

        # Count and log PDFs surfaced only by the broad-keyword fallback scorer.
        # via_broad_scorer=True means _score_link gave 0 and _score_pdf_link gave >0.
        broad_pdfs_this_uni = sum(1 for c in candidates if c.get("via_broad_scorer"))
        total_broad_scorer_pdfs += broad_pdfs_this_uni
        if broad_pdfs_this_uni:
            log.info(
                "[RECOVERY] uni_id=%s — %d PDF candidate(s) surfaced via broad-keyword "
                "fallback scorer (would have been missed by standard link scorer)",
                uni_id, broad_pdfs_this_uni,
            )

        # URLs discovered via the broad-keyword fallback — their PDF results will
        # be tagged source_type='pdf_broad' so the summary API can count them.
        url_is_broad: set[str] = {
            c["url"] for c in candidates if c.get("via_broad_scorer")
        }

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

        # Per-category PDF budget shared across all candidate-URL fetches for this
        # university.  Each category (fees, english, …) has its own independent
        # counter so a university with many fees PDFs cannot crowd out
        # english-requirements PDF fetches in the same pass.
        pdf_budget: dict[str, int] = make_pdf_budget(single_course=False)

        # Shared dedup set: a PDF URL that appears both as a direct candidate
        # and as a linked PDF on another HTML page must only be downloaded once.
        seen_pdf_urls: set[str] = set()

        extracted_by_category: dict[str, list[dict[str, Any]]] = {}
        for url, url_cats in url_to_categories.items():
            try:
                page_results = await extract_from_url(
                    url, url_cats, country=country,
                    pdf_budget=pdf_budget, seen_pdf_urls=seen_pdf_urls,
                )
                # Tag PDF results from broad-scorer-discovered URLs as 'pdf_broad'
                # so operators can distinguish them from standard PDF results.
                if url in url_is_broad:
                    for r in page_results:
                        if r.get("source_type") == "pdf":
                            r["source_type"] = "pdf_broad"
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
        "pdfs_via_broad_scorer": total_broad_scorer_pdfs,
    }
    log.info("[RECOVERY] pass complete for run %r — %s", scrape_run_id, summary)
    if total_broad_scorer_pdfs:
        log.info(
            "[RECOVERY] run %r — %d PDF(s) discovered via broad-keyword fallback scorer "
            "(tagged source_type='pdf_broad' in recovery results)",
            scrape_run_id, total_broad_scorer_pdfs,
        )
    if emit:
        broad_note = (
            f", {total_broad_scorer_pdfs} via broad-keyword PDF scorer"
            if total_broad_scorer_pdfs else ""
        )
        await emit(
            "status",
            f"[RECOVERY] Agent Recovery complete — {total_examined} courses examined, "
            f"{total_written} recovery results written{broad_note}",
            phase="recovery",
            **summary,
        )
    return summary


async def run_single_course_recovery(
    scraped_course_id: int,
    db: AsyncSession,
) -> list[dict[str, Any]]:
    """Run a fresh recovery pass for a single staged course.

    Used by the API trigger endpoint.  Deletes all non-final rows before
    re-running so operators always see fresh results.  Writes diagnostic
    trace rows at each failure point so the panel shows WHY recovery found
    nothing (not just a silent empty state).

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
    from app.services.scraper.recovery.extractor import (
        extract_from_url,
        make_pdf_budget,
    )
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

    # Delete all non-final rows (pending + all trace statuses) so we start fresh.
    # Applied and rejected rows are preserved — they represent operator decisions.
    await db.execute(
        text(
            "DELETE FROM agent_recovery_results "
            "WHERE scraped_course_id = :sc_id "
            "  AND status NOT IN ('applied', 'rejected')"
        ),
        {"sc_id": scraped_course_id},
    )
    await db.commit()

    # ------------------------------------------------------------------ #
    # STEP 1 — BFS search for candidate pages                             #
    # ------------------------------------------------------------------ #
    candidates = await search_candidate_pages(scrape_url, needed_categories)
    if not candidates:
        log.info("[RECOVERY] trigger: course %s — no candidates found", scraped_course_id)
        # Write a no_source trace for every needed field
        for field in needed:
            await _write_trace_row(
                db, scraped_course_id, scrape_run_id, field,
                "no_source",
                "No candidate pages found during BFS domain search for this field category",
            )
        await db.commit()
        return await _fetch_all_rows_for_course(db, scraped_course_id)

    # ------------------------------------------------------------------ #
    # STEP 2 — Fetch each URL once, run all relevant extractors           #
    # ------------------------------------------------------------------ #
    url_to_categories: dict[str, set[str]] = {}
    for cand in candidates:
        url_to_categories.setdefault(cand["url"], set()).add(cand["category"])

    # URLs discovered via the broad-keyword fallback scorer — their PDF results
    # will be tagged source_type='pdf_broad' so the summary API can count them.
    url_is_broad: set[str] = {c["url"] for c in candidates if c.get("via_broad_scorer")}
    if url_is_broad:
        log.info(
            "[RECOVERY] trigger: course %s — %d URL(s) found via broad-keyword fallback scorer",
            scraped_course_id, len(url_is_broad),
        )

    # Track which categories had candidates at all
    categories_searched: set[str] = {cand["category"] for cand in candidates}
    # Track source_type per URL (html_empty = both HTTP and browser failed)
    url_source_types: dict[str, str] = {}
    # Accumulate all extracted results
    all_results: list[dict[str, Any]] = []
    # Track which categories produced at least one result with a non-None value
    categories_with_results: set[str] = set()

    # Per-course PDF budget — tighter than the batch cap because a single-course
    # trigger only needs a handful of PDFs to fill its missing fields.
    pdf_budget: dict[str, int] = make_pdf_budget(single_course=True)

    for url, url_cats in url_to_categories.items():
        meta: dict[str, str] = {}
        try:
            page_results = await extract_from_url(
                url, url_cats, country=country, metadata=meta, pdf_budget=pdf_budget
            )
            url_source_types[url] = meta.get("source_type", "html")
            # Tag PDF results from broad-scorer-discovered URLs as 'pdf_broad'
            if url in url_is_broad:
                for r in page_results:
                    if r.get("source_type") == "pdf":
                        r["source_type"] = "pdf_broad"
            for r in page_results:
                if r.get("value") is not None:
                    cat = FIELD_TO_CATEGORY.get(r.get("field", ""), "")
                    if cat:
                        categories_with_results.add(cat)
            all_results.extend(page_results)
        except Exception as exc:
            log.warning("[RECOVERY] trigger extract error %r: %s", url, exc)
            url_source_types[url] = "html_empty"

    # ------------------------------------------------------------------ #
    # STEP 3 — Map results to the course (with rejection tracking)        #
    # ------------------------------------------------------------------ #
    mapped, rejects = map_results_to_course(
        all_results,
        degree_level=sc.degree_level,
        course_name=sc.course_name,
        return_rejects=True,
    )
    mapped_for_course = {f: v for f, v in mapped.items() if f in needed}

    # Write the successful recovery results first
    await _write_recovery_results(db, scraped_course_id, scrape_run_id, mapped_for_course)

    # ------------------------------------------------------------------ #
    # STEP 4 — Write diagnostic trace rows for every needed field that    #
    # was NOT successfully recovered                                       #
    # ------------------------------------------------------------------ #
    # Fields whose extractor returned a non-None value (may still have been
    # mapper-rejected)
    fields_with_extracted_value = {
        r.get("field") for r in all_results if r.get("value") is not None
    }

    for field in needed:
        if field in mapped_for_course:
            continue  # Successfully recovered — no trace needed

        cat = FIELD_TO_CATEGORY.get(field)

        # Which URLs were candidates for this field's category?
        cat_urls = [
            url for url, url_cats in url_to_categories.items()
            if cat in url_cats
        ] if cat else []

        # URL-level failure signals
        browser_failed_urls = [
            url for url in cat_urls
            if url_source_types.get(url) == "html_empty"
        ]
        pdf_urls = [
            url for url in cat_urls
            if url_source_types.get(url) in ("pdf_direct", "pdf_content_type")
        ]

        if field in rejects:
            # The extractor found a value, but the mapper disqualified it
            best_rej = rejects[field][0]
            extracted_val = best_rej.get("value")
            evidence = f"Extracted: {extracted_val}" if extracted_val else None
            await _write_trace_row(
                db, scraped_course_id, scrape_run_id, field,
                "level_mismatch",
                best_rej.get("reason", "Value found but rejected by degree-level check"),
                source_url=best_rej.get("source_url"),
                evidence_text=evidence,
            )

        elif browser_failed_urls:
            # Fetch failed for the URL(s) covering this field's category
            await _write_trace_row(
                db, scraped_course_id, scrape_run_id, field,
                "browser_failed",
                "Page fetch failed — site may require JavaScript rendering or "
                "Cloudflare protection blocked the request",
                source_url=browser_failed_urls[0],
            )

        elif pdf_urls and field not in fields_with_extracted_value:
            # A PDF URL was identified but extraction returned no data
            await _write_trace_row(
                db, scraped_course_id, scrape_run_id, field,
                "pdf_failed",
                "PDF found but text extraction returned no usable data for this field",
                source_url=pdf_urls[0],
            )

        elif cat and cat not in categories_with_results:
            # Candidates were found for this category but extractor found nothing
            await _write_trace_row(
                db, scraped_course_id, scrape_run_id, field,
                "no_value",
                "Candidate pages were found but no value for this field could be "
                "extracted from the page content",
                source_url=cat_urls[0] if cat_urls else None,
            )

        else:
            # Other fields in the same category were extracted, but not this one
            await _write_trace_row(
                db, scraped_course_id, scrape_run_id, field,
                "no_value",
                "Related fields were extracted from the page but this specific "
                "field was not found",
                source_url=cat_urls[0] if cat_urls else None,
            )

    await db.commit()
    return await _fetch_all_rows_for_course(db, scraped_course_id)
