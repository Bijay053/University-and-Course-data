"""Sibling-cache back-fill (T206).

Many universities publish a single English-language requirement table
that applies to *all* undergraduate (or all postgraduate) courses, then
omit those scores from individual course pages. The per-course
extractors honestly emit nothing, the AI fallback can't invent values,
and the rows stage with empty IELTS/PTE/TOEFL/CAE — even though one of
their siblings did extract the table successfully.

Mirrors Node's ``backfillEnglishFromSiblings`` (routes/scrape.ts:9381).
The bucket key is the degree-level group ("undergraduate" /
"postgraduate") so a Bachelor's table never bleeds into a Doctorate
row. Within a bucket we take the *modal* value (most-frequent across
siblings) so a one-off outlier doesn't drag the whole bucket.

Public entry-point: :func:`backfill_english_from_siblings`. It runs
AFTER the per-course extract phase, BEFORE staging — that ordering is
load-bearing because the cache must observe the high-quality slot
values from siblings that did manage to extract them.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any, Awaitable, Callable, Final

log = logging.getLogger(__name__)

_ENGLISH_SLOTS: Final = (
    "ielts_overall",
    "ielts_listening",
    "ielts_reading",
    "ielts_writing",
    "ielts_speaking",
    "pte_overall",
    "pte_listening",
    "pte_reading",
    "pte_writing",
    "pte_speaking",
    "toefl_overall",
    "toefl_listening",
    "toefl_reading",
    "toefl_writing",
    "toefl_speaking",
    "cambridge_overall",
    "duolingo_overall",
)

# Subset of _ENGLISH_SLOTS that the sibling-cache is permitted to backfill.
#
# PTE, TOEFL, Cambridge, and Duolingo scores are institution-wide equivalences,
# NOT per-course values.  ACU (and many other universities) publish only the
# IELTS requirement on individual course pages; the PTE/TOEFL/CAE/DET
# equivalences live on a central Admissions policy page that may not map
# cleanly to each course's true minimum.  Backfilling these from siblings
# causes every course to show the same non-IELTS scores even when those tests
# are never mentioned on the course page itself, flagged by data-quality checks
# as inconsistent (e.g. IELTS 6.0 + Cambridge 176 = IELTS 7.0 threshold gap).
#
# Rule: sibling-cache may only propagate IELTS scores. PTE / TOEFL / CAE /
# Duolingo are left null unless they were explicitly on the course page itself.
# This applies globally across all universities.
_SIBLING_BACKFILL_SLOTS: Final = (
    "ielts_overall",
    "ielts_listening",
    "ielts_reading",
    "ielts_writing",
    "ielts_speaking",
)

_UNDERGRAD_HINTS: Final = (
    "bachelor", "undergraduate", "diploma", "certificate", "associate",
    "foundation", "bridging", "honours",
)
_POSTGRAD_HINTS: Final = (
    "master", "postgraduate", "graduate certificate",
    "graduate diploma",
)
# Research/doctoral degrees are kept in their own bucket so they never
# inherit English requirements from taught/coursework programmes.
#
# At universities like Flinders, PhD admission is supervisor-driven and
# course pages carry no English requirement section at all.  Putting PhDs
# in the "postgraduate" bucket alongside taught Masters caused every PhD
# row to inherit IELTS=6 from the Masters pool — fabricated data that
# looked credible and would have been approved by admins.
#
# With a dedicated "research" bucket: if no PhD/research page in the run
# yields an IELTS value, the research bucket cache is empty and nothing
# is backfilled (correct blank).  At universities where PhD pages DO
# publish IELTS requirements, research degrees backfill from each other
# only — still correct.
_RESEARCH_HINTS: Final = (
    "doctor", "phd", "doctorate", "by research", "master of philosophy",
    "mphil", "research degree",
)


def _bucket_for(payload: dict[str, Any]) -> str:
    """Return ``"undergraduate"``, ``"postgraduate"``, ``"research"`` or
    ``"unknown"``.

    Looks at ``degree_level`` first (cheap, set by the degree_level
    extractor) then falls back to keyword-matching the course name. The
    "unknown" bucket is its own pool — it's better to share a value
    among the unknowns than to splash a Master's score onto an
    unidentified Bachelor's.

    Research/doctoral degrees are checked before postgraduate so that
    "Doctor of Philosophy" is never merged into the taught-postgraduate
    pool.
    """
    lvl = (payload.get("degree_level") or "").lower()
    # Research must be checked before postgraduate — "doctor"/"phd" must
    # not fall through into the postgraduate branch.
    if any(h in lvl for h in _RESEARCH_HINTS):
        return "research"
    if any(h in lvl for h in _POSTGRAD_HINTS):
        return "postgraduate"
    if any(h in lvl for h in _UNDERGRAD_HINTS):
        return "undergraduate"
    name = (payload.get("course_name") or "").lower()
    if any(h in name for h in _RESEARCH_HINTS):
        return "research"
    if any(h in name for h in _POSTGRAD_HINTS):
        return "postgraduate"
    if any(h in name for h in _UNDERGRAD_HINTS):
        return "undergraduate"
    return "unknown"


# Week 1 Prompt 4 — sibling cache source-type gating.
#
# Only values produced by high-precision extractors are allowed to seed the
# bucket cache.  Lower-precision sources (vision OCR, AI fallback, central-
# page tables, prior sibling-cache rounds) frequently misread context — a
# single hallucination would otherwise propagate to every sibling course.
#
# The check runs at CACHE-WRITE time inside :func:`_build_bucket_cache`, so
# excluded values neither vote nor become the modal value.  Read-path /
# backfill behaviour is unchanged.
#
# Coupled with ``_MIN_CACHE_CONFIDENCE`` below: even a permitted source
# method must report confidence ≥ 0.7 to seed the cache.
# Methods explicitly disallowed from seeding the cache: low-precision /
# self-referential / cross-level extractors that have a known history of
# propagating bad values.  Anything matching this set is rejected.
_DISALLOWED_CACHE_SOURCES: Final[frozenset[str]] = frozenset({
    "ai_fallback",            # AI text generation — Prompt 8 caller-side guarded
    "vision_ocr",             # generic vision OCR — global tier-1 suppression P7B
    "vision_ocr:tier1",       # explicit tier-1 vision (non-DOM-anchored)
    "approved_row:inherited", # value carried over from a prior approved row
    "sibling_cache_backfill", # circular — would feed the cache from itself
})

# Method-name PREFIXES that are explicitly disallowed.  Captures all
# vision_ocr:* and sibling_cache:* variants regardless of the suffix.
_DISALLOWED_CACHE_PREFIXES: Final[tuple[str, ...]] = (
    "vision_ocr",      # vision_ocr, vision_ocr:tier1, vision_ocr:gemini ...
    "sibling_cache",   # any prior sibling_cache:* round
    "ai_fallback",     # ai_fallback:* (defensive — none today)
)

# Methods explicitly allowed (after the prefix-blocklist above is applied):
# methods/prefixes from extractors known to read static page data with
# high precision.  Anything not on this allowlist also fails the gate —
# better to silently skip seeding than to propagate an unknown source.
#
# Audit source: rg '"method":' app/services/scraper/ (2026-05-09).
_ALLOWED_CACHE_PREFIXES: Final[tuple[str, ...]] = (
    "regex",                    # regex:cricos, regex:* — deterministic
    "css_selector",             # CSS-selector-based extractors
    "gemini_primary",           # Gemini structured extraction (high-conf)
    "bond_static",              # Bond JSON-API static extractor
    "csu_static",               # CSU static-page extractor
    "ecu_static",               # ECU static-page extractor
    "vit_static_fallback",      # VIT static fallback (deterministic)
    "per_course_browser",       # Playwright-rendered per-course extraction
    "per_course_modal",         # Playwright modal extraction
    "central_page:english_level",  # per-degree-level central English table
    "uni_pdf:requirements",     # PDF-parsed requirements (anchored)
    "program_level_table",      # program-level structured table
    "equivalence_table",        # equivalence-table extractor
    "study_mode",               # study_mode:* deterministic derivations
    "category",                 # category:* (rule + det)
)
_MIN_CACHE_CONFIDENCE: Final[float] = 0.7


def _can_seed_cache(method: str, conf: float) -> bool:
    """Return True iff this evidence record may seed the sibling cache.

    Two-stage gate:

    1. Reject anything whose method is on the disallow list (or starts
       with a disallow-prefix).  This keeps tier-1 vision OCR,
       AI fallback, and sibling-cache-derived values out of the cache
       regardless of confidence.
    2. Accept only methods that match an entry in
       :data:`_ALLOWED_CACHE_PREFIXES` AND whose ``conf`` clears the
       :data:`_MIN_CACHE_CONFIDENCE` threshold.

    Returning False is non-fatal: the evidence row still appears in the
    payload, it simply doesn't vote when the bucket cache is built.
    """
    if not method:
        return False
    if conf < _MIN_CACHE_CONFIDENCE:
        return False
    if method in _DISALLOWED_CACHE_SOURCES:
        return False
    for bad in _DISALLOWED_CACHE_PREFIXES:
        if method == bad or method.startswith(bad + ":"):
            return False
    for ok in _ALLOWED_CACHE_PREFIXES:
        if method == ok or method.startswith(ok + ":"):
            return True
    return False


_CROSS_LEVEL_METHODS: Final = (
    # central_page:english fills every course in the scrape with the same
    # university-level requirement — it may reflect only one degree-level's
    # numbers (e.g. plain HTTP fetch returns Bachelor's table only).  Using
    # these values to build the bucket cache would seed every bucket with the
    # same wrong value; they may still be useful as a last-resort fallback
    # applied directly to courses that have NO other English data, but they
    # must NOT be used to drive the sibling-cache vote.
    "central_page:english",
    # sibling_cache: values are themselves derived from previous bucket votes
    # — including them would cause circular reinforcement of stale data.
    # The prefix covers all variants: sibling_cache:undergraduate,
    # sibling_cache:postgraduate, sibling_cache:unknown.
    "sibling_cache:",
)


def _is_cross_level_method(method: str) -> bool:
    return any(method.startswith(pfx) for pfx in _CROSS_LEVEL_METHODS)


def _build_bucket_cache(
    results: list[dict[str, Any]],
    *,
    min_quorum: int = 2,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, str]]]:
    """Build ``{bucket: {slot: most_common_value}}`` and the matching origin URLs.

    Returns a 2-tuple ``(cache, origins)`` where:

    * ``cache`` — ``{bucket: {slot: value}}`` — the modal value per slot per bucket
    * ``origins`` — ``{bucket: {slot: origin_course_url}}`` — the URL of the
      first course whose evidence contributed the winning (modal) value; used
      by :func:`backfill_english_from_siblings` to write evidence rows that
      point back to the actual source course, not the recipient course.

    Ignores empty / None slots and only emits a slot when at least one
    sibling has a value (so back-fill never invents data). Ties on
    frequency are broken by Counter's insertion order, which matches
    the per-course gather() return order — deterministic across runs.

    Values that came from ``central_page:english`` or a previous
    ``sibling_cache:*`` round are excluded from the vote: the former may
    reflect only one degree-level's requirements (causing cross-level
    contamination), and the latter would create circular reinforcement.
    Only values extracted directly from per-course pages (HTML, PDF,
    browser, AI, vision) are eligible to seed the cache.

    Parameters
    ----------
    min_quorum
        Minimum number of distinct courses in a bucket that must agree on
        a value before it is promoted to the cache.  Default is 1 (original
        behaviour: any single value wins).  Set to 2 or higher for
        universities where accidental extraction on a non-course page could
        otherwise seed the entire bucket with a wrong value (e.g. Bond, where
        marketing pages can mention IELTS 6.5 in text that the extractor
        misreads).
    """
    buckets: dict[str, dict[str, Counter]] = {}
    # Track the first course URL + method + confidence that contributed each
    # (bucket, slot, value).  Stored as (url, method, conf) tuples so
    # backfill evidence rows can record full provenance for audit purposes.
    first_origin: dict[str, dict[str, dict[Any, tuple[str, str, float]]]] = {}

    for r in results:
        if not isinstance(r, dict):
            continue
        payload = r.get("payload") or {}
        course_url: str = r.get("url") or ""
        bucket = _bucket_for(payload)
        slot_counters = buckets.setdefault(
            bucket, {k: Counter() for k in _SIBLING_BACKFILL_SLOTS}
        )
        bucket_origins = first_origin.setdefault(
            bucket, {k: {} for k in _SIBLING_BACKFILL_SLOTS}
        )
        # Build a quick lookup: field_key → (method, confidence) from the
        # evidence that actually set the payload value. First-write-wins in
        # the pipeline means the earliest evidence entry for a key is the
        # authoritative one.
        evidence_method: dict[str, str] = {}
        evidence_conf: dict[str, float] = {}
        for ev in r.get("evidence") or []:
            fk = ev.get("field_key", "")
            if fk and fk not in evidence_method:
                evidence_method[fk] = ev.get("method", "")
                try:
                    evidence_conf[fk] = float(ev.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    evidence_conf[fk] = 0.0

        for k in _SIBLING_BACKFILL_SLOTS:
            v = payload.get(k)
            if v in (None, "", 0):
                continue
            _method = evidence_method.get(k, "")
            _conf = evidence_conf.get(k, 0.0)
            # Skip values that came from cross-level fallbacks.
            if _is_cross_level_method(_method):
                continue
            # Week 1 Prompt 4 — source-type gate.  Only regex / css_selector /
            # gemini_primary at conf ≥ 0.7 may seed the cache; vision OCR,
            # AI fallback and other low-precision methods would otherwise
            # propagate hallucinated values to every sibling course.
            if not _can_seed_cache(_method, _conf):
                log.debug(
                    "[SIBLING CACHE GATE] dropping seed candidate %s=%r — "
                    "method=%s conf=%.2f (allowlist=%s, conf>=%.2f) url=%s",
                    k, v, _method or "unknown", _conf,
                    list(_ALLOWED_CACHE_PREFIXES), _MIN_CACHE_CONFIDENCE,
                    course_url,
                )
                continue
            slot_counters[k][v] += 1
            # Record the first course URL + source method + confidence that
            # contributed this (slot, value) for provenance evidence rows.
            if v not in bucket_origins[k]:
                bucket_origins[k][v] = (
                    course_url,
                    evidence_method.get(k, "unknown"),
                    evidence_conf.get(k, 0.0),
                )

    # _ProvMeta holds everything needed to write a complete provenance row.
    # Keys: source_url, source_method, source_conf, consensus_count.
    _ProvMeta = dict[str, Any]

    cache: dict[str, dict[str, Any]] = {}
    origins: dict[str, dict[str, str]] = {}
    prov_meta: dict[str, dict[str, _ProvMeta]] = {}
    for bucket, counters in buckets.items():
        slot_values: dict[str, Any] = {}
        slot_origins: dict[str, str] = {}
        slot_prov: dict[str, _ProvMeta] = {}
        bucket_first = first_origin.get(bucket) or {}
        for k, counter in counters.items():
            if not counter:
                continue
            most_common, count = counter.most_common(1)[0]
            # Require at least min_quorum courses to agree before promoting.
            # When min_quorum=2 and only 1 course in the bucket extracted an
            # English score (e.g. a footer-mention on a non-course page),
            # the value is not backfilled to all 50+ siblings.
            if count < min_quorum:
                continue
            origin_tuple = (bucket_first.get(k) or {}).get(most_common, ("", "unknown", 0.0))
            src_url, src_method, src_conf = (
                origin_tuple if isinstance(origin_tuple, tuple) else (origin_tuple, "unknown", 0.0)
            )
            slot_values[k] = most_common
            slot_origins[k] = src_url
            slot_prov[k] = {
                "source_url": src_url,
                "source_method": src_method,
                "source_conf": round(src_conf, 4),
                "consensus_count": count,
            }
        if slot_values:
            cache[bucket] = slot_values
            origins[bucket] = slot_origins
            prov_meta[bucket] = slot_prov
    return cache, origins, prov_meta


# Mapping from test prefix → the payload flag key that indicates whether
# the university accepts that test.  NULL = unknown; False = not accepted;
# True = explicitly accepted.  The sibling cache must not backfill a slot
# whose test the university has declared it does not accept.
_SLOT_ACCEPTED_FLAG: Final[dict[str, str]] = {
    slot: flag
    for prefix, flag in (
        ("toefl",     "toefl_accepted"),
        ("pte",       "pte_accepted"),
        ("cambridge", "cambridge_accepted"),
        ("duolingo",  "duolingo_accepted"),
    )
    for slot in _ENGLISH_SLOTS
    if slot.startswith(prefix)
}


async def backfill_english_from_siblings(
    results: list[dict[str, Any]],
    *,
    emit: Callable[..., Awaitable[None]] | None = None,
    min_quorum: int = 2,
) -> int:
    """Mutate ``results`` in place, filling empty english slots from
    same-bucket siblings.

    Returns the total number of slot-fills performed across the run so
    the orchestrator can fold it into its summary metrics. Per-bucket
    log lines are emitted as
    ``[EXTRACT] [sibling cache ↻ backfill <bucket>] ielts_overall=6.5
    pte_overall=58 ...``.

    Parameters
    ----------
    min_quorum
        Passed through to :func:`_build_bucket_cache`. A value of 2 prevents
        a single page (e.g. a marketing page that mentions "IELTS 6.5" in
        text) from seeding the cache and backfilling every sibling in the run.

        Week 1 Prompt 6 — default raised from 1 → 2 globally.  Trades
        coverage for correctness: any field whose only source is a single
        sibling is now left null rather than propagated to every sibling
        in the bucket.  The orchestrator may pass a higher value for
        universities with known false-positive sources.
    """
    cache, cache_origins, cache_prov = _build_bucket_cache(results, min_quorum=min_quorum)
    if not cache:
        return 0

    if emit:
        for bucket, slot_values in cache.items():
            scores = " ".join(
                f"{k}={v}" for k, v in sorted(slot_values.items())
            )
            await emit(
                "status",
                f"[EXTRACT] [sibling cache ↻ build {bucket}] {scores}",
                phase="extract",
                kind="sibling_cache_build",
                bucket=bucket,
                values=dict(slot_values),
            )

    fills_total = 0
    backfilled_per_bucket: dict[str, int] = {}
    for r in results:
        if not isinstance(r, dict):
            continue
        payload = r.get("payload") or r.setdefault("payload", {})
        evidence = r.setdefault("evidence", [])
        bucket = _bucket_for(payload)
        slot_values = cache.get(bucket) or {}
        if not slot_values:
            continue
        bucket_prov = cache_prov.get(bucket) or {}
        # Fallback URL for evidence rows when origin tracking found no URL.
        course_url: str = r.get("url") or ""
        for k, v in slot_values.items():
            existing = payload.get(k)
            if existing not in (None, "", 0):
                continue
            # Respect *_accepted flags: if the university has explicitly
            # declared it does not accept this test (False), skip the slot
            # entirely so we don't publish scores for a test the university
            # rejects (e.g. ACAP doesn't accept Duolingo — duolingo_accepted=False).
            accepted_flag = _SLOT_ACCEPTED_FLAG.get(k)
            if accepted_flag and payload.get(accepted_flag) is False:
                continue
            payload[k] = v
            # Pull full provenance from the cache-build phase.
            prov = bucket_prov.get(k) or {}
            origin_url: str = prov.get("source_url") or course_url
            source_method: str = prov.get("source_method") or "unknown"
            source_conf: float = prov.get("source_conf") or 0.0
            consensus_count: int = prov.get("consensus_count") or 1
            # Encode full provenance as a JSON object in the snippet field so
            # it is queryable via PostgreSQL's JSONB operators:
            #   snippet::jsonb->>'source_method'
            #   snippet::jsonb->>'consensus_count'
            snippet_doc = {
                "method": "sibling_cache_backfill",
                "field": k,
                "value": v,
                "bucket": bucket,
                "source_url": origin_url or "unknown",
                "source_method": source_method,
                "source_conf": source_conf,
                "consensus_count": consensus_count,
            }
            evidence.append(
                {
                    "field_key": k,
                    "value": v,
                    "confidence": 0.55,
                    "method": f"sibling_cache:{bucket}",
                    # origin_url is the course that originally yielded this
                    # value; course_url is the recipient being backfilled.
                    "source_url": origin_url,
                    # Extra provenance keys — stored in snippet as JSON so
                    # they survive into scraped_field_evidence.snippet and
                    # are queryable without a schema migration.
                    "source_method": source_method,
                    "source_conf": source_conf,
                    "bucket": bucket,
                    "consensus_count": consensus_count,
                    "snippet": json.dumps(snippet_doc, separators=(",", ":")),
                }
            )
            fills_total += 1
            backfilled_per_bucket[bucket] = backfilled_per_bucket.get(bucket, 0) + 1

    if emit and backfilled_per_bucket:
        for bucket, n in backfilled_per_bucket.items():
            await emit(
                "status",
                f"[EXTRACT] [sibling cache ↻ backfill {bucket}] "
                f"{n} slot(s) filled across siblings",
                phase="extract",
                kind="sibling_cache_backfill",
                bucket=bucket,
                fills=n,
            )
    return fills_total
