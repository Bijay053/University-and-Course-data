"""Run all extractors over one course page and return a merged record.

Output shape is keyed for direct insertion into ``scraped_courses`` via
``stage_course``. Each extractor's ``normalized`` payload contributes
fields; a missing extractor simply leaves its slot empty.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.services.scraper.category import classify_category, map_course_to_category
from app.services.scraper.guards import should_trust_generic_university_fee_fallback
from app.services.scraper.extractors import (
    ai_fallback,
    course_name,
    degree_level,
    duration,
    eligibility,
    english_test,
    fee,
    intake,
    location,
    study_mode,
)
from app.services.scraper.extractors.base import ExtractionResult
from app.services.scraper.http_fetcher import fetch_html
from app.services.scraper.provenance import build_course_page_provenance_footer

log = logging.getLogger(__name__)


# Hard ceiling on the AI fallback Gemini call. Same bug class as the
# Playwright hang that started this hot-fix chain — if Gemini stalls
# (network, model-side queueing, retries inside the SDK), we would
# freeze a whole worker. PR-1.5 prod regression on VIT showed the 60s
# ceiling firing on multiple courses (`AI fallback exceeded 60s on
# https://vit.edu.au/mba — moving on without AI fill`) when the prompt
# had to fill many missing fields against a long page; bumping to 120s
# matches the Node-era timeout and gives a vision-capable Gemini call
# room to finish a multi-field extract on a heavy page (typical 10–25s,
# worst-case 60–90s during a model-side queueing event).
_AI_FALLBACK_TIMEOUT_SEC = 120


def _apply_ai_duration_mapping(payload: dict[str, Any], ai_filled: dict[str, Any]) -> None:
    """Translate AI's `duration_value` / `duration_unit` keys into the
    canonical `duration` / `duration_term` keys used by the staged-course
    schema. Mutates ``ai_filled`` in place. Only fills when the rule
    extractor hasn't already populated the canonical key, so a confident
    regex hit always beats an AI guess. See B20 root-cause notes."""
    if "duration" not in payload and ai_filled.get("duration_value") is not None:
        try:
            ai_filled["duration"] = float(ai_filled["duration_value"])
        except (TypeError, ValueError):
            pass
    if "duration_term" not in payload and ai_filled.get("duration_unit"):
        from app.services.scraper.extractors.duration import _normalise_unit
        term = _normalise_unit(str(ai_filled["duration_unit"]))
        if term:
            ai_filled["duration_term"] = term


# Each entry: (module, kwargs the extractor accepts beyond html/url).
# degree_level + study_mode were missing before Bug C — without them the
# Review table's Level / Mode columns showed "--" for every staged course
# and auto_publish_status was permanently stuck on "pending_review".
_EXTRACTORS = (
    (course_name, ()),
    (location, ()),
    (eligibility, ()),
    (fee, ("country",)),
    (english_test, ()),
    (intake, ()),
    (duration, ()),
    (degree_level, ()),
    (study_mode, ()),
)


async def extract_course(
    url: str,
    *,
    country: str | None = None,
    html: str | None = None,
    use_ai_fallback: bool = True,
    uni_pdf_data: dict[str, Any] | None = None,
    emit=None,
) -> dict[str, Any]:
    """Fetch (if needed) and run all extractors. Returns merged payload + raw evidence.

    ``uni_pdf_data`` is the (optional) result of
    :func:`app.services.scraper.pipelines.university_pdfs.load_university_pdf_data`,
    used as a *last-resort* fallback for fee/IELTS fields that the per-page
    extractors and AI fallback could not fill.
    """
    if html is None:
        html = await fetch_html(url)
    if not html:
        return {"url": url, "error": "fetch_failed", "payload": {}, "evidence": []}

    payload: dict[str, Any] = {"course_website": url}
    evidence: list[dict[str, Any]] = []

    for module, extra_keys in _EXTRACTORS:
        kwargs: dict[str, Any] = {}
        for k in extra_keys:
            if k == "country":
                kwargs["country"] = country
        # degree_level accepts an optional ``course_name`` so it can read
        # the H1-level title without re-parsing <title>. Pass whatever the
        # course_name extractor already produced (it runs first in the
        # tuple, so payload['course_name'] is populated by the time we
        # reach degree_level). Falls through harmlessly when the kwarg
        # isn't supported by this extractor.
        if module is degree_level and payload.get("course_name"):
            kwargs["course_name"] = payload["course_name"]
        try:
            results: list[ExtractionResult] = await module.extract(html, url, **kwargs)
        except Exception as exc:  # one extractor must never break the others
            log.warning("Extractor %s failed on %s: %s", module.__name__, url, exc)
            continue
        for r in results:
            evidence.append(
                {
                    "field_key": r.field_key,
                    "value": r.value,
                    "confidence": r.confidence,
                    "method": r.method,
                    "snippet": r.snippet,
                }
            )
            if r.normalized:
                for k, v in r.normalized.items():
                    if v is None:
                        continue
                    # First-write-wins so the highest-confidence result (which
                    # the extractor returned first) is preserved.
                    payload.setdefault(k, v)

    # T207/T208: per-course browser + vision fallback. Run BEFORE the AI
    # fallback because (a) they're cheaper, (b) AI hallucinates plausible
    # numbers when the page is image-only and the browser/vision pass
    # provides ground truth that AI can use as additional context (when
    # we then run AI). Both helpers are no-ops when the english slots
    # are already populated.
    rendered_html: str | None = None
    try:
        from app.services.scraper.per_course_browser import maybe_browser_refetch
        from app.services.scraper.per_course_vision import maybe_vision_refetch

        browser_filled, browser_evidence, rendered_html = await maybe_browser_refetch(
            url, payload, emit=emit
        )
        for k, v in browser_filled.items():
            payload.setdefault(k, v)
        evidence.extend(browser_evidence)

        vision_filled, vision_evidence = await maybe_vision_refetch(
            url, rendered_html, payload, emit=emit
        )
        for k, v in vision_filled.items():
            payload.setdefault(k, v)
        evidence.extend(vision_evidence)
    except Exception as exc:  # noqa: BLE001 — never break extraction here
        log.warning("per-course browser/vision fallback errored on %s: %s", url, exc)

    if use_ai_fallback:
        # Note which slots are still empty so the UI can show *what* the AI
        # is being asked to fill (helpful when diagnosing weak per-page
        # extraction on a new university template).
        _ai_target_keys = (
            "international_fee", "domestic_fee", "ielts_overall",
            "duration_text", "intake_text", "location_text",
        )
        missing = [k for k in _ai_target_keys if k not in payload or payload.get(k) is None]
        if emit:
            await emit(
                "status",
                f"[FALLBACK] AI enriching {url} (missing: {', '.join(missing) if missing else 'none'})",
                phase="extract",
                kind="ai_fallback_start",
                missing=missing,
            )
        try:
            # Hard ceiling so a hung Gemini call cannot wedge a worker
            # the same way the Playwright incident did. On timeout the
            # underlying SDK call is cancelled and we fall through to
            # the existing "AI failure" path — extraction proceeds
            # without AI fill, which is the same UX as a model error.
            ai_filled = await asyncio.wait_for(
                ai_fallback.fill_missing(payload, html=html, url=url),
                timeout=_AI_FALLBACK_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            log.warning(
                "AI fallback exceeded %ss on %s — aborting this course's AI pass",
                _AI_FALLBACK_TIMEOUT_SEC,
                url,
            )
            if emit:
                await emit(
                    "status",
                    f"[FALLBACK] AI fallback exceeded "
                    f"{_AI_FALLBACK_TIMEOUT_SEC}s on {url} — moving on without AI fill",
                    phase="extract",
                    kind="ai_fallback_timeout",
                    timeout_seconds=_AI_FALLBACK_TIMEOUT_SEC,
                    level="warn",
                )
            ai_filled = {}
        except Exception as exc:  # never break extraction on AI failure
            log.warning("AI fallback errored on %s: %s", url, exc)
            if emit:
                await emit(
                    "status",
                    f"[FALLBACK] AI fallback errored on {url}: {exc}",
                    phase="extract",
                    kind="ai_fallback_error",
                )
            ai_filled = {}
        if emit and ai_filled:
            await emit(
                "status",
                f"[FALLBACK] AI filled {len(ai_filled)} field(s) on {url}: "
                f"{', '.join(ai_filled.keys())}",
                phase="extract",
                kind="ai_fallback_done",
                filled=list(ai_filled.keys()),
            )
        # AI returns duration as `duration_value` + `duration_unit` (kept
        # separate so the prompt can constrain each field independently).
        # The staged-course schema uses `duration` (real) +
        # `duration_term` (Year/Month/Week/...). Translate before merging
        # so AI-filled units don't silently drop on the floor. See B20.
        _apply_ai_duration_mapping(payload, ai_filled)
        for k, v in ai_filled.items():
            payload.setdefault(k, v)
            evidence.append(
                {
                    "field_key": k,
                    "value": v,
                    "confidence": 0.5,
                    "method": "ai_fallback",
                    "snippet": None,
                }
            )

    # Last-resort: backfill from university-level PDFs (fee schedule,
    # admissions/IELTS policy). Only fills keys still missing after
    # page extractors + AI. Each filled key emits a provenance row that
    # credits the source PDF URL.
    if uni_pdf_data:
        fee_block = uni_pdf_data.get("fee") or {}
        english_block = uni_pdf_data.get("english") or {}
        fees_pdf_url = uni_pdf_data.get("fees_pdf_url")
        reqs_pdf_url = uni_pdf_data.get("requirements_pdf_url")

        # Diff item H (MIGRATION_AUDIT.md §6): gate the uni-wide fee PDF
        # fallback on course-specific evidence. Without this, every
        # Bachelor on the catalogue inherits the same single dollar
        # amount from the generic /international-fees page (Torrens v1
        # symptom).
        #
        # The guard is text-based, so we can only run it when the loader
        # surfaces ``fee_text`` (the raw extracted PDF text we'd grep for
        # course-name tokens). Today ``load_university_pdf_data`` only
        # returns the parsed numbers, not the source text — wiring that
        # through is a follow-up. Until then, fail-OPEN when no text is
        # available (preserves v1 behavior) and fail-CLOSED only when the
        # caller has supplied text we can actually evaluate against.
        # (Code-review feedback on PR-1: avoid silently dropping every
        # uni-PDF fallback now that we lack the text channel.)
        fee_search_text = uni_pdf_data.get("fee_text") or ""
        fee_amount = fee_block.get("international_fee")
        unique_amounts = (
            [int(fee_amount)] if isinstance(fee_amount, (int, float)) else []
        )
        trust_fee_fallback = True
        if fee_block and fees_pdf_url and fee_search_text:
            try:
                trust_fee_fallback = should_trust_generic_university_fee_fallback(
                    fees_pdf_url,
                    payload.get("course_name") or "",
                    fee_search_text,
                    unique_amounts,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("fee-guard failed for %s: %s", fees_pdf_url, exc)
                trust_fee_fallback = False
            if not trust_fee_fallback:
                log.info(
                    "[FEE] uni-PDF fallback skipped for %s — no course-specific evidence",
                    payload.get("course_name"),
                )

        if trust_fee_fallback:
            for k, v in fee_block.items():
                if v is None or k in payload:
                    continue
                payload[k] = v
                evidence.append(
                    {
                        "field_key": k,
                        "value": v,
                        "confidence": 0.7,
                        "method": "uni_pdf:fees",
                        "snippet": fees_pdf_url,
                    }
                )
        for k, v in english_block.items():
            if v is None or k in payload:
                continue
            payload[k] = v
            evidence.append(
                {
                    "field_key": k,
                    "value": v,
                    "confidence": 0.7,
                    "method": "uni_pdf:requirements",
                    "snippet": reqs_pdf_url,
                }
            )

    # Rule-based category classifier — runs after every other slot is
    # populated so we can use the (possibly AI-filled) course_name. The
    # Review table's Category column reads scraped_courses.category; without
    # this step every row showed NULL. Skip if an extractor already produced
    # a category (none currently do, but keeps the pipeline future-proof).
    cname = payload.get("course_name") or ""
    # T204: keyword-based pre-map sets BOTH category and sub_category from
    # well-known compound titles ("Hospitality Management" → Tourism &
    # Hospitality / Hospitality Management). Runs first; the body-text
    # classify_category fallback only fires when no pre-map keyword hit.
    det = map_course_to_category(cname)
    if det:
        if not payload.get("category"):
            payload["category"] = det["category"]
            evidence.append(
                {
                    "field_key": "category",
                    "value": det["category"],
                    "confidence": 0.7,
                    "method": "category:det",
                    "snippet": cname,
                }
            )
        if not payload.get("sub_category"):
            payload["sub_category"] = det["sub_category"]
            evidence.append(
                {
                    "field_key": "sub_category",
                    "value": det["sub_category"],
                    "confidence": 0.7,
                    "method": "category:det",
                    "snippet": cname,
                }
            )
        if emit:
            await emit(
                "status",
                f"[CATEGORY det] {cname[:40]} → {det['category']} / {det['sub_category']}",
                phase="classify",
            )
    if not payload.get("category"):
        cat = classify_category(cname)
        if cat:
            payload["category"] = cat
            evidence.append(
                {
                    "field_key": "category",
                    "value": cat,
                    "confidence": 0.6,
                    "method": "category:rule",
                    "snippet": cname,
                }
            )

    footer = build_course_page_provenance_footer(payload)
    return {
        "url": url,
        "payload": payload,
        "evidence": evidence,
        "provenance_footer": footer,
    }
