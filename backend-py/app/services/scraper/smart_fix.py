"""Bounded, issue-aware adapter around the existing staged recovery pipeline."""
import asyncio
import time


SUPPORTED_TARGETS = frozenset({
    "ielts_overall", "international_fee", "course_location", "study_mode",
    "duration", "intake_months", "academic_level", "other_requirement",
    "academic_score", "english_requirements", "course_name",
})


def central_recovery_config(config, targets):
    """Only already-configured official central sources, never guessed URLs."""
    keys = set()
    if "international_fee" in targets:
        keys.update({"feePage", "feesPdf"})
    if "english_requirements" in targets or any(f.startswith("ielts_") for f in targets):
        keys.update({"entryPage", "requirementsPage", "entryPageUG", "entryPagePG"})
    pages = {
        key: value for key, value in (config.get("uniPages") or {}).items()
        if key in keys and value
    }
    return {**config, "uniPages": pages} if pages else None


async def refresh_central_recovery(config, targets, previous, timeout):
    """One fresh read can bypass a stale cache; identical evidence is not retried."""
    from app.services.scraper.central_pages import prefetch_central_pages
    from app.services.scraper_config_ai import _is_safe_public_url

    narrowed = central_recovery_config(config, targets)
    if narrowed is None or timeout <= 0:
        return None
    deadline = time.monotonic() + min(timeout, 45)
    # Reuse the existing public-address guard before the fresh cache-bypass read.
    # These are exact configured URLs, including explicitly configured external
    # document hosts; this is not permission to discover or follow new sources.
    for url in set(narrowed["uniPages"].values()):
        safe, _ = await asyncio.wait_for(
            asyncio.to_thread(_is_safe_public_url, url),
            timeout=min(5, max(0.01, deadline - time.monotonic())),
        )
        if not safe:
            raise ValueError("Configured central recovery source is not a public HTTP URL")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    fresh = await asyncio.wait_for(
        prefetch_central_pages(narrowed, university_id=None), timeout=remaining,
    )
    # Compare only categories this recovery requested: absent unrelated categories
    # must not turn a narrowed result into apparent new evidence.
    relevant_keys = (
        {"fees", "fee_page_url"} if "international_fee" in targets else set()
    )
    if any(k in narrowed["uniPages"] for k in (
        "entryPage", "requirementsPage", "entryPageUG", "entryPagePG",
    )):
        relevant_keys.update({"english", "english_by_level", "english_by_program",
                              "english_page_url"})
    changed = {
        key: value for key, value in fresh.items()
        if key in relevant_keys and value and value != (previous or {}).get(key)
    }
    return {**(previous or {}), **changed} if changed else None


def smart_retry_fields(targeted_fields, row, payload):
    """A new central profile cannot replace improvements from the first pass."""
    from app.services.scraper.extractors.ulaw_fees import validated_fee_variants
    owned_fee = validated_fee_variants({
        "course_website": getattr(row, "course_website", None), **payload,
    })
    return {
        field for field in targeted_fields
        if not payload.get(field) or payload.get(field) == getattr(row, field, None)
        if not (field in {"international_fee", "fee_year", "fee_term", "currency"} and owned_fee)
    }


def plan_targets(issues, requested, forced=()):
    """Only analyzer-recognized identities may be automatically targeted."""
    unresolved = {
        item["field"] for item in issues
        if item.get("missing", 0) > 0 and item.get("field") in SUPPORTED_TARGETS
    }
    selected = unresolved & set(requested) if requested else unresolved
    return sorted(selected | set(forced))


async def run_smart_batch(body, db):
    from app.routers.scrape import ReExtractBody, analyze_staged, re_extract_staged

    deadline = time.monotonic() + 240
    results = []
    for course_id in body.ids:
        scope = ReExtractBody(ids=[course_id], universityId=body.university_id)
        try:
            before = await analyze_staged(scope, db)
        except Exception:
            await db.rollback()
            results.append({
                "id": course_id, "ok": False, "attempted": False,
                "made_progress": False, "resolved_fields": [],
                "unresolved_fields": sorted(set(body.target_fields) | set(body.force_fields)),
                "reason_code": "initial_analysis_failed",
                "error": "Could not check the current course issues. Reload and retry.",
            })
            continue
        targets = plan_targets(before["issues"], body.target_fields, body.force_fields)
        unsupported = sorted(set(body.target_fields) - SUPPORTED_TARGETS)
        base = {
            "id": course_id, "target_fields": targets, "attempted": False,
            "unsupported_fields": unsupported,
        }
        if not before["total"]:
            results.append({
                **base, "ok": False, "error": "Selected staged course no longer exists",
                "reason_code": "course_not_found",
            })
            continue
        if not targets:
            results.append({
                **base, "ok": True, "made_progress": False,
                "resolved_fields": [], "unresolved_fields": [],
                "reason_code": "unsupported_targets" if unsupported else "already_resolved",
                "reason": (
                    "Some selected fields have no supported automatic issue check."
                    if unsupported else "No unresolved issues remain in the selected scope."
                ),
            })
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.append({
                **base, "ok": False, "error": "Smart Fix batch budget exhausted",
                "reason_code": "budget_exhausted", "unresolved_fields": targets,
            })
            continue
        if not before["courses_with_url"]:
            results.append({
                **base, "ok": True, "made_progress": False,
                "reason_code": "missing_official_url",
                "reason": "No course source URL is saved. Submit the exact official course URL.",
                "unresolved_fields": targets, "resolved_fields": [],
                "next_action": "report_official_url",
            })
            continue
        try:
            part = await asyncio.wait_for(re_extract_staged(
                ReExtractBody(
                    ids=[course_id], universityId=body.university_id,
                    targetFields=targets, forceFields=body.force_fields,
                    forceReasons=body.force_reasons, smart=True,
                ), db,
            ), timeout=remaining)
        except TimeoutError:
            await db.rollback()
            results.append({
                **base, "attempted": True, "ok": False,
                "error": "Smart Fix batch budget exhausted",
                "reason_code": "budget_exhausted", "unresolved_fields": targets,
            })
            continue
        except Exception:
            await db.rollback()
            results.append({
                **base, "attempted": True, "ok": False,
                "error": "Official-source recovery failed; retry or report the official URL.",
                "reason_code": "source_recovery_failed", "unresolved_fields": targets,
                "next_action": "report_official_url",
            })
            continue
        item = next(
            (dict(result) for result in part.get("results") or []
             if result.get("id") == course_id),
            {"id": course_id, "ok": False, "error": "Recovery returned no matching result"},
        )
        item.update({
            **base, "attempted": True, "made_progress": False,
            "resolved_fields": [], "unresolved_fields": targets,
        })
        if not item.get("ok"):
            item["reason_code"] = "source_recovery_failed"
            results.append(item)
            continue
        try:
            after = await analyze_staged(scope, db)
            if after.get("total") != 1:
                item.update({
                    "ok": False, "reason_code": "course_changed_during_fix",
                    "error": "The selected course disappeared or changed scope during Fix. Reload the review.",
                })
                results.append(item)
                continue
            unresolved = set(plan_targets(after["issues"], targets))
        except Exception:
            await db.rollback()
            item.update({
                "ok": False, "reason_code": "post_analysis_failed",
                "error": "Recovery ran, but its issue resolution could not be verified. Reload and re-check.",
            })
            results.append(item)
            continue
        # Force corrections can change values without resolving an analyzer issue.
        before_issues = set(plan_targets(before["issues"], targets))
        authoritative_corrections = set()
        if (
            "international_fee" in body.force_fields
            and "international_fee" in item.get("progress_fields", [])
            and "international_fee" not in unresolved
        ):
            from app.models import ScrapedCourse
            from app.services.scraper.extractors.ulaw_fees import validated_fee_variants
            corrected_row = await db.get(ScrapedCourse, course_id)
            if corrected_row is not None and validated_fee_variants(corrected_row):
                authoritative_corrections.add("international_fee")
        resolved = sorted((before_issues - unresolved) | authoritative_corrections)
        item.update({
            **base, "attempted": True,
            "resolved_fields": resolved, "unresolved_fields": sorted(unresolved),
            "made_progress": bool(resolved),
        })
        if not item.get("ok"):
            item["reason_code"] = "source_recovery_failed"
        elif unresolved:
            item["reason_code"] = "unresolved_after_official_recovery"
            item["reason"] = (
                "Official-source recovery did not resolve all selected issues. "
                "Submit an exact official URL supporting the unresolved fields."
            )
            item["next_action"] = "report_official_url"
        elif resolved:
            item["reason_code"] = "issues_resolved"
        else:
            item["reason_code"] = "correction_requires_review"
            item["reason"] = "Correction attempted; review the selected source before accepting it."
        results.append(item)
    return {"results": results}