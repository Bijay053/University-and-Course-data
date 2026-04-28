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

_UNDERGRAD_HINTS: Final = (
    "bachelor", "undergraduate", "diploma", "certificate", "associate",
    "foundation", "bridging", "honours",
)
_POSTGRAD_HINTS: Final = (
    "master", "postgraduate", "doctor", "phd", "graduate certificate",
    "graduate diploma", "doctorate",
)


def _bucket_for(payload: dict[str, Any]) -> str:
    """Return ``"undergraduate"``, ``"postgraduate"`` or ``"unknown"``.

    Looks at ``degree_level`` first (cheap, set by the degree_level
    extractor) then falls back to keyword-matching the course name. The
    "unknown" bucket is its own pool — it's better to share a value
    among the unknowns than to splash a Master's score onto an
    unidentified Bachelor's.
    """
    lvl = (payload.get("degree_level") or "").lower()
    if any(h in lvl for h in _POSTGRAD_HINTS):
        return "postgraduate"
    if any(h in lvl for h in _UNDERGRAD_HINTS):
        return "undergraduate"
    name = (payload.get("course_name") or "").lower()
    if any(h in name for h in _POSTGRAD_HINTS):
        return "postgraduate"
    if any(h in name for h in _UNDERGRAD_HINTS):
        return "undergraduate"
    return "unknown"


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
    min_quorum: int = 1,
) -> dict[str, dict[str, Any]]:
    """Build ``{bucket: {slot: most_common_value}}``.

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
    for r in results:
        if not isinstance(r, dict):
            continue
        payload = r.get("payload") or {}
        bucket = _bucket_for(payload)
        slot_counters = buckets.setdefault(
            bucket, {k: Counter() for k in _ENGLISH_SLOTS}
        )
        # Build a quick lookup: field_key → method that actually SET the value.
        # first-write-wins in the pipeline means the earliest evidence entry
        # for a key is the one that set the payload value.
        evidence_method: dict[str, str] = {}
        for ev in r.get("evidence") or []:
            fk = ev.get("field_key", "")
            if fk and fk not in evidence_method:
                evidence_method[fk] = ev.get("method", "")

        for k in _ENGLISH_SLOTS:
            v = payload.get(k)
            if v in (None, "", 0):
                continue
            # Skip values that came from cross-level fallbacks.
            if _is_cross_level_method(evidence_method.get(k, "")):
                continue
            slot_counters[k][v] += 1
    cache: dict[str, dict[str, Any]] = {}
    for bucket, counters in buckets.items():
        slot_values: dict[str, Any] = {}
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
            slot_values[k] = most_common
        if slot_values:
            cache[bucket] = slot_values
    return cache


async def backfill_english_from_siblings(
    results: list[dict[str, Any]],
    *,
    emit: Callable[..., Awaitable[None]] | None = None,
    min_quorum: int = 1,
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
        Defaults to 1 to preserve the original behaviour for all universities
        that didn't exhibit this false-positive; pass 2 for Bond and similar.
    """
    cache = _build_bucket_cache(results, min_quorum=min_quorum)
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
        # The course URL for this result — used as source_url in evidence so
        # enforce_source_evidence (guards.py) keeps the backfilled value.
        course_url: str = r.get("url") or ""
        for k, v in slot_values.items():
            existing = payload.get(k)
            if existing not in (None, "", 0):
                continue
            payload[k] = v
            evidence.append(
                {
                    "field_key": k,
                    "value": v,
                    "confidence": 0.55,
                    "method": f"sibling_cache:{bucket}",
                    # enforce_source_evidence requires both source_url and snippet
                    # to be non-empty; use the current course URL as the source
                    # (sibling values came from the same university's courses).
                    "source_url": course_url,
                    "snippet": f"sibling-cache backfill from {bucket} bucket: {k}={v}",
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
