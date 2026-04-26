"""Run all extractors over one course page and return a merged record.

Output shape is keyed for direct insertion into ``scraped_courses`` via
``stage_course``. Each extractor's ``normalized`` payload contributes
fields; a missing extractor simply leaves its slot empty.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-checking-only import to avoid pulling per_course_vision (and
    # its heavy gemini_client transitive imports) at module load time.
    # The real runtime import happens lazily inside ``extract_course``
    # alongside the other per_course_* fallbacks.
    from app.services.scraper.per_course_vision import VisionImageCache  # noqa: F401

from app.services.scraper.category import classify_category, map_course_to_category
from app.services.scraper.guards import should_trust_generic_university_fee_fallback
from app.services.scraper.extractors import (
    ai_fallback,
    course_name,
    degree_level,
    description,
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

# Degree-level values that indicate a postgraduate course.
# The central English-requirements page is fetched via plain HTTP (no JS
# rendering), so it only captures whatever level the static HTML exposes
# first — typically the undergraduate table.  Applying those UG values to
# PG courses produces incorrect (too-low) English scores.
# Courses at these levels are exempt from the central_page:english fallback;
# they will stage with NULL English scores rather than wrong Bachelor's values.
# NULL is always recoverable; wrong data propagates silently.
_CENTRAL_ENGLISH_PG_LEVELS: frozenset[str] = frozenset({
    "Master's",
    "Graduate Certificate",
    "Graduate Diploma",
    "Doctorate",
})


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


# PR-5 Bug 1 was a postgrad-IELTS bump heuristic against the uni-PDF
# backfill — REVERTED. The bump masked the real problem: course-page
# english data is sometimes a screenshot image (e.g. ASA Bachelor of
# Business publishes the english table as PNG only), so the per-course
# extractor fills nothing and the uni-PDF backfill — which holds a
# single bachelor-tier value — gets stamped on every course. Bumping by
# +0.5 IELTS made masters look plausible without being correct (real
# masters minimums vary 6.0–7.5 by program). The course-page-wins
# precedence is already enforced (this function and sibling_cache both
# skip if the slot is non-empty). The right fix lives elsewhere: OCR
# the image, parse a per-degree-level PDF, or surface the gap as
# "needs review" rather than synthesising a number.


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
    (description, ()),   # meta/p description — runs early on static HTML
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
    vision_image_cache: "VisionImageCache | None" = None,
    central_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch (if needed) and run all extractors. Returns merged payload + raw evidence.

    ``uni_pdf_data`` is the (optional) result of
    :func:`app.services.scraper.pipelines.university_pdfs.load_university_pdf_data`,
    used as a *last-resort* fallback for fee/IELTS fields that the per-page
    extractors and AI fallback could not fill.

    ``central_data`` is the (optional) result of
    :func:`app.services.scraper.central_pages.prefetch_central_pages`, used as
    the *absolute last-resort* fallback (lower confidence than ``uni_pdf_data``)
    for universities that publish fees/IELTS only on central pages (Bug 2).
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

    # ── Bug 1 (KBS): location-based mode correction ──────────────────────────
    # The bare `\bonline\b` fallback in study_mode.py fires on marketing copy
    # like "Apply Online" / "Enquire Online" found in footers/navs of pages
    # that have NO structural mode label.  It is assigned confidence=0.5
    # (deliberately low) but still wins when there's no competing signal.
    #
    # If course_location has content (location extractor already strips
    # virtual/online keywords), the course has a physical campus.  A bare
    # `\bonline\b` hit at confidence ≤ 0.5 must NOT override that evidence.
    # Similarly, if mode wasn't determined at all but we have a location,
    # derive "On Campus" rather than leaving the field blank.
    _study_mode_evidence = [e for e in evidence if e["field_key"] == "study_mode"]
    _low_conf_online = (
        payload.get("study_mode") == "Online"
        and any(
            e.get("confidence", 1.0) <= 0.5 and e.get("method") == "study_mode:rule"
            for e in _study_mode_evidence
        )
    )
    _mode_absent = not payload.get("study_mode")
    if _mode_absent or _low_conf_online:
        from app.services.scraper.extractors.study_mode import derive_mode_from_location

        _derived_mode = derive_mode_from_location(payload.get("course_location"))
        if _derived_mode:
            payload["study_mode"] = _derived_mode
            evidence.append(
                {
                    "field_key": "study_mode",
                    "value": _derived_mode,
                    "confidence": 0.6,
                    "method": "study_mode:location_derived",
                    "snippet": (
                        f"Derived from course_location: "
                        f"{(payload.get('course_location') or '')[:80]}"
                    ),
                }
            )

    # T002: per-course Bootstrap-modal English-test extractor. Runs BEFORE
    # the per-course browser pass because (a) it's pure-CPU (no Playwright
    # spin-up, no network), (b) the english_test extractor often misses
    # the values when they live ONLY inside a hidden modal, and (c) a
    # successful modal pass populates the english slots so the browser
    # fallback no-ops on its first gate. Only fires when at least one
    # english slot is still empty — paying for BeautifulSoup parse on a
    # page whose IELTS already extracted is wasted work.
    _ENGLISH_SLOTS_FOR_MODAL = (
        "ielts_overall", "pte_overall", "toefl_overall", "cambridge_overall",
    )
    if any(payload.get(k) in (None, "", 0) for k in _ENGLISH_SLOTS_FOR_MODAL):
        try:
            from app.services.scraper.per_course_modal import extract_modal_english

            modal_filled = extract_modal_english(
                html,
                course_name=payload.get("course_name") or "",
                degree_level=payload.get("degree_level") or "",
            )
            modal_summary = modal_filled.pop("__modal_summary", None)
            for k, v in modal_filled.items():
                if v in (None, "", 0):
                    continue
                if k in payload and payload.get(k) not in (None, "", 0):
                    continue
                payload[k] = v
                evidence.append(
                    {
                        "field_key": k,
                        "value": v,
                        "confidence": 0.9,
                        "method": "per_course_modal",
                        "snippet": modal_summary,
                    }
                )
            if emit and modal_filled:
                await emit(
                    "status",
                    f"[per-course modal ✓] {payload.get('course_name', url)[:40]} — "
                    f"{modal_summary or ''}",
                    phase="extract",
                    kind="per_course_modal_done",
                    url=url,
                    filled=list(modal_filled.keys()),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("per_course_modal failed on %s: %s", url, exc)
            if emit:
                await emit(
                    "status",
                    f"[per-course modal ✗] {payload.get('course_name', url)[:40]} — "
                    f"{str(exc)[:80]}",
                    phase="extract",
                    kind="per_course_modal_error",
                    url=url,
                )

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
            url, rendered_html, payload, emit=emit,
            image_cache=vision_image_cache,
        )
        for k, v in vision_filled.items():
            payload.setdefault(k, v)
        evidence.extend(vision_evidence)
    except Exception as exc:  # noqa: BLE001 — never break extraction here
        log.warning("per-course browser/vision fallback errored on %s: %s", url, exc)

    # T003: VIT-specific static fallback for duration / intake / location.
    # The per-course browser pass clicks the "International students"
    # toggle which strips the static narrative paragraph (`<p><strong>
    # Duration:</strong> Usually a 3 year course...</p>`) from the
    # rendered DOM. We re-parse the original static HTML to recover
    # those fields. Only fires when at least one of the three slots is
    # still missing AND the URL is a vit.edu.au page.
    try:
        from app.services.scraper.vit_static_extract import (
            apply_vit_summary_extraction,
            is_vit_url,
        )
        if is_vit_url(url):
            need_dur = payload.get("duration") in (None, "", 0) or not payload.get("duration_term")
            need_int = payload.get("intake_text") in (None, "")
            need_loc = payload.get("location_text") in (None, "")
            if need_dur or need_int or need_loc:
                vit_filled = apply_vit_summary_extraction(url, html, payload)
                for k, v in vit_filled.items():
                    if v in (None, "", 0):
                        continue
                    if payload.get(k) not in (None, "", 0):
                        continue
                    payload[k] = v
                    evidence.append(
                        {
                            "field_key": k,
                            "value": v,
                            "confidence": 0.85,
                            "method": "vit_static_fallback",
                            "snippet": None,
                        }
                    )
                if emit and vit_filled:
                    parts = []
                    if vit_filled.get("duration") is not None:
                        parts.append(
                            f"duration={vit_filled.get('duration')}"
                            f"{vit_filled.get('duration_term', '')}"
                        )
                    if vit_filled.get("intake_text"):
                        parts.append(f"intakes={vit_filled['intake_text']}")
                    if vit_filled.get("location_text"):
                        parts.append(f"location={vit_filled['location_text']}")
                    await emit(
                        "status",
                        f"[VIT static fallback ✓] "
                        f"{payload.get('course_name', url)[:40]} — "
                        f"recovered {', '.join(parts)}",
                        phase="fallback",
                        kind="vit_static_done",
                        url=url,
                        filled=list(vit_filled.keys()),
                    )
    except Exception as exc:  # noqa: BLE001
        log.warning("vit_static_extract failed on %s: %s", url, exc)

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

        # NEW: prefer the per-course row from the fee schedule PDF over
        # the uni-wide value. ``fee_by_course`` is populated when the
        # PDF was a multi-row schedule (ASA, Torrens, …). Matching is
        # done by distinctive course-name tokens — see
        # :func:`match_course_in_pdf_table`. When a row matches, it
        # *replaces* ``fee_block`` for this course and is tagged with a
        # different provenance method so reviewers can tell per-course
        # rows apart from the old uni-wide stamp.
        fee_by_course = uni_pdf_data.get("fee_by_course") or {}
        fee_method = "uni_pdf:fees"
        # When the schedule PDF parses to a real per-course table (≥2
        # rows — same threshold ``_pick_per_course_amounts`` uses to
        # consider a table "real"), the per-course path becomes the
        # source of truth for this university's fees. Falling back to
        # the uni-wide stamp for unmatched courses re-creates the
        # original failure mode this PR was built to fix (every course
        # gets the same number) — Torrens v1 symptom. We instead leave
        # the fee NULL so the dashboard surfaces it as missing rather
        # than silently wrong.
        per_course_table_active = len(fee_by_course) >= 2
        if fee_by_course:
            from app.services.scraper.pipelines.university_pdfs import (
                match_course_in_pdf_table,
            )

            matched_row = match_course_in_pdf_table(
                payload.get("course_name") or "", fee_by_course
            )
            if matched_row:
                log.info(
                    "[FEE] per-course PDF row matched for %r: $%s (%s)",
                    payload.get("course_name"),
                    matched_row.get("international_fee"),
                    matched_row.get("fee_term"),
                )
                fee_block = matched_row
                fee_method = "uni_pdf:fees:per_course"
            elif per_course_table_active:
                # No per-course row matched, but the schedule itself
                # parses cleanly. Suppress the uni-wide stamp so we
                # don't pollute every unmatched course with the same
                # (likely-wrong) number. Leave the rest of the PDF
                # block (english requirements, etc.) intact.
                log.info(
                    "[FEE] no per-course PDF row for %r — leaving fee NULL "
                    "(schedule has %d rows; uni-wide stamp suppressed)",
                    payload.get("course_name"),
                    len(fee_by_course),
                )
                fee_block = {}

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
        # The guard is intentionally bypassed when we have a per-course
        # row — that row IS the course-specific evidence the guard is
        # asking for, so applying the guard a second time would be
        # double-jeopardy.
        fee_search_text = uni_pdf_data.get("fee_text") or ""
        fee_amount = fee_block.get("international_fee")
        unique_amounts = (
            [int(fee_amount)] if isinstance(fee_amount, (int, float)) else []
        )
        trust_fee_fallback = True
        if (
            fee_block
            and fees_pdf_url
            and fee_search_text
            and fee_method == "uni_pdf:fees"  # only guard the uni-wide stamp
        ):
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
                if v is None:
                    continue
                # Empty-aware: only skip when the page already extracted
                # a *real* value (matches per-course modal / VIT static /
                # sibling cache). Treats None / "" / 0 as "still empty"
                # so a stray placeholder from any upstream merge site
                # never blocks the PDF backfill. Course-page-wins still
                # holds — see step-1 extractors which strip Nones before
                # setdefault, so a real extraction is always truthy.
                if payload.get(k) not in (None, "", 0):
                    continue
                payload[k] = v
                evidence.append(
                    {
                        "field_key": k,
                        "value": v,
                        "confidence": 0.7,
                        "method": fee_method,
                        "snippet": fees_pdf_url,
                    }
                )
        # Course-page-wins: only fill empty english slots from the
        # uni-PDF backfill. The PDF's value gets stored verbatim — no
        # bump or other heuristic. If the PDF only publishes a bachelor
        # tier, masters courses end up with that tier; that's a known
        # gap to be solved upstream (per-degree-level PDF parsing or
        # OCR of course-page screenshots), NOT by guessing here.
        for k, v in english_block.items():
            if v is None:
                continue
            # Empty-aware: see fee-block comment above.
            if payload.get(k) not in (None, "", 0):
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

    # Bug 2: central-pages fallback — applies fees and English requirements
    # pre-fetched from a university's central fee/admissions page when
    # per-course extractors, AI, and PDF backfill all left these slots empty.
    # This is the absolute last-resort path: confidence ceiling is 0.45 for
    # fees (lower than every earlier stage) and 0.50 for English requirements
    # (central admissions pages are authoritative for English policy, but we
    # still want course-page data and sibling cache to win when present).
    if central_data:
        try:
            from app.services.scraper.central_pages import match_central_fee

            _central_fees: list = central_data.get("fees") or []
            _central_english: dict = central_data.get("english") or {}
            _central_fee_url: str | None = central_data.get("fee_page_url")
            _central_eng_url: str | None = central_data.get("english_page_url")

            # ── Fee fallback ─────────────────────────────────────────────
            _fee_slots = ("international_fee", "domestic_fee", "currency", "fee_term", "fee_year")
            _fee_missing = any(payload.get(k) in (None, "", 0) for k in ("international_fee",))
            if _fee_missing and _central_fees:
                matched = match_central_fee(
                    payload.get("course_name") or "",
                    _central_fees,
                    degree_level=payload.get("degree_level"),
                )
                if matched:
                    _filled_fee_keys: list[str] = []
                    for _k, _src_k in (
                        ("international_fee", "international_fee"),
                        ("domestic_fee", "domestic_fee"),
                        ("currency", "currency"),
                        ("fee_term", "per"),
                    ):
                        _v = matched.get(_src_k)
                        if _v in (None, "", 0):
                            continue
                        if payload.get(_k) not in (None, "", 0):
                            continue
                        payload[_k] = _v
                        evidence.append({
                            "field_key": _k,
                            "value": _v,
                            "confidence": 0.45,
                            "method": "central_page:fees",
                            "snippet": _central_fee_url,
                        })
                        _filled_fee_keys.append(_k)
                    if emit and _filled_fee_keys:
                        _prog = matched.get("program_pattern", "?")
                        await emit(
                            "status",
                            f"[CENTRAL ✓] {payload.get('course_name', url)[:40]} — "
                            f"fee from '{_prog}' row: "
                            f"intl={matched.get('international_fee')} "
                            f"per={matched.get('per')}",
                            phase="fallback",
                            kind="central_fee_applied",
                            url=url,
                            matched_program=_prog,
                            filled=_filled_fee_keys,
                        )

            # ── English-requirements fallback ────────────────────────────
            # Two data paths, in priority order:
            #
            # Path 1 — level-keyed data (``english_by_level``): populated when
            #   ``central_english_pg_skip`` is True and the English page was
            #   browser-rendered.  Contains separate dicts for "undergraduate"
            #   and "postgraduate".  Apply the bucket that matches this course's
            #   degree_level.  No skip needed — the values are already correct.
            #
            # Path 2 — flat data (``english``): populated for all universities.
            #   For universities where the central page is level-uniform (KBS,
            #   most others) this is the right value for every course.
            #   For ASA-style pages it reflects UG-only values (6.0/50/60/169);
            #   applying them to PG courses is wrong, hence the pg_skip flag.
            _course_dl = (payload.get("degree_level") or "").strip()
            _english_by_level: dict = central_data.get("english_by_level") or {}
            _level_bucket = (
                "postgraduate"
                if _course_dl in _CENTRAL_ENGLISH_PG_LEVELS
                else "undergraduate"
            )
            _level_english: dict = _english_by_level.get(_level_bucket) or {}

            # Path 1: level-specific values available — use them unconditionally.
            if _level_english:
                _eng_filled: list[str] = []
                for _k, _v in _level_english.items():
                    if _v in (None, "", 0):
                        continue
                    if payload.get(_k) not in (None, "", 0):
                        continue
                    payload[_k] = _v
                    evidence.append({
                        "field_key": _k,
                        "value": _v,
                        "confidence": 0.55,
                        "method": "central_page:english_level",
                        "snippet": _central_eng_url,
                    })
                    _eng_filled.append(_k)
                if emit and _eng_filled:
                    _scores = " ".join(
                        f"{k.replace('_overall', '')}={payload.get(k)}"
                        for k in _eng_filled
                    )
                    await emit(
                        "status",
                        f"[CENTRAL ✓] {payload.get('course_name', url)[:40]} — "
                        f"english ({_level_bucket}) from central page: {_scores}",
                        phase="fallback",
                        kind="central_english_level_applied",
                        url=url,
                        bucket=_level_bucket,
                        filled=_eng_filled,
                    )

            # Path 2: fall back to flat values when no level-keyed data exists.
            else:
                _pg_skip_configured = bool(
                    central_data.get("central_english_pg_skip", False)
                )
                _skip_central_english = (
                    _pg_skip_configured and _course_dl in _CENTRAL_ENGLISH_PG_LEVELS
                )
                if _central_english and not _skip_central_english:
                    _eng_filled = []
                    for _k, _v in _central_english.items():
                        if _v in (None, "", 0):
                            continue
                        if payload.get(_k) not in (None, "", 0):
                            continue
                        payload[_k] = _v
                        evidence.append({
                            "field_key": _k,
                            "value": _v,
                            "confidence": 0.50,
                            "method": "central_page:english",
                            "snippet": _central_eng_url,
                        })
                        _eng_filled.append(_k)
                    if emit and _eng_filled:
                        _scores = " ".join(
                            f"{k.replace('_overall', '')}={payload.get(k)}"
                            for k in _eng_filled
                        )
                        await emit(
                            "status",
                            f"[CENTRAL ✓] {payload.get('course_name', url)[:40]} — "
                            f"english from central page: {_scores}",
                            phase="fallback",
                            kind="central_english_applied",
                            url=url,
                            filled=_eng_filled,
                        )
                elif _central_english and _skip_central_english and emit:
                    await emit(
                        "status",
                        f"[CENTRAL —] {payload.get('course_name', url)[:40]} — "
                        f"central english skipped for PG level ({_course_dl or 'unknown'}): "
                        f"no level-keyed data, pg_skip=true",
                        phase="fallback",
                        kind="central_english_skipped_pg",
                        url=url,
                        degree_level=_course_dl,
                    )

        except Exception as exc:  # noqa: BLE001 — never abort extraction
            log.warning("central_pages fallback errored on %s: %s", url, exc)

        # ── PG English clear-out (safety net) ────────────────────────────────
        # When ``central_english_pg_skip`` is True AND the browser fetch did
        # not return reliable level-keyed PG data (``english_by_level``
        # missing or has no "postgraduate" entry), any English scores that
        # landed in the payload are unreliable.  NULL is honest and
        # recoverable; a silently-wrong 6.0 for a Master's that requires
        # 6.5 is neither.
        #
        # When the browser DID return level-keyed data and Path 1 applied
        # the correct PG values above, this block is skipped — the values
        # are already right and should not be cleared.
        #
        # This runs AFTER all extractors have settled (including vision OCR
        # and sibling-cache backfill) so it is the definitive last word.
        _pg_skip_final = bool(central_data.get("central_english_pg_skip", False))
        _pg_dl_final = (payload.get("degree_level") or "").strip()
        _pg_has_level_data = bool(
            (central_data.get("english_by_level") or {}).get("postgraduate")
        )
        if (
            _pg_skip_final
            and not _pg_has_level_data
            and _pg_dl_final in _CENTRAL_ENGLISH_PG_LEVELS
        ):
            _cleared: list[str] = []
            for _slot in ("ielts_overall", "pte_overall", "toefl_overall", "cambridge_overall"):
                if payload.get(_slot) not in (None, "", 0):
                    payload[_slot] = None
                    _cleared.append(_slot)
            if _cleared and emit:
                await emit(
                    "status",
                    f"[PG-SKIP ✗] {payload.get('course_name', url)[:40]} — "
                    f"nulled english for PG ({_pg_dl_final}): "
                    f"{', '.join(_cleared)} (no level-keyed data from browser)",
                    phase="fallback",
                    kind="pg_english_cleared",
                    url=url,
                    degree_level=_pg_dl_final,
                    cleared=_cleared,
                )

        # Signal to the staging gate that this university has a centralized fee
        # page.  Even if this specific course wasn't listed in the table, the
        # course may still be open to international students — the staging gate
        # should stage it for human review rather than auto-rejecting it.
        if central_data.get("fee_page_url"):
            payload["has_central_fee_page"] = True

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
