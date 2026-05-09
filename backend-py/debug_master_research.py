"""
[MASTER_RESEARCH_TRACE] End-to-end field trace for:
  https://www.unisq.edu.au/study/degrees-and-courses/master-of-research?studentType=international

Run with:
  cd backend-py && PYTHONPATH=. python debug_master_research.py
"""
import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)

TARGET_URL = (
    "https://www.unisq.edu.au/study/degrees-and-courses/"
    "master-of-research?studentType=international"
)
TRACE_FIELDS = [
    "international_fee",
    "ielts_overall",
    "duration_text",
    "intake_text",
    "duration",
    "duration_term",
    "intake_months",
]


def _snap(label, payload):
    vals = {k: payload.get(k) for k in TRACE_FIELDS}
    print("\n[MASTER_RESEARCH_TRACE] " + ("=" * 55))
    print("[MASTER_RESEARCH_TRACE] " + label)
    print("[MASTER_RESEARCH_TRACE] " + ("=" * 55))
    for k, v in vals.items():
        print("[MASTER_RESEARCH_TRACE]   {:25s} = {!r}".format(k, v))


async def run():
    import app.services.scraper.per_course_browser as _pcb
    import app.services.scraper.guards as _guards

    # ── Patch _extended_extract to log what browser finds / skips ────────────
    _orig_extended = _pcb._extended_extract

    async def _traced_extended(rendered, url, existing_payload, override=False):
        filled, evidence = await _orig_extended(
            rendered, url, existing_payload, override=override
        )
        print("\n[MASTER_RESEARCH_TRACE] _extended_extract (override={})".format(override))
        for k in TRACE_FIELDS:
            if k in filled:
                print("[MASTER_RESEARCH_TRACE]   BROWSER FOUND   {:25s} = {!r}".format(
                    k, filled[k]))
            elif existing_payload.get(k) not in (None, "", 0, []):
                print("[MASTER_RESEARCH_TRACE]   BROWSER SKIPPED {:25s} (existing={!r})".format(
                    k, existing_payload.get(k)))
            else:
                print("[MASTER_RESEARCH_TRACE]   BROWSER EMPTY   {:25s}".format(k))
        return filled, evidence

    _pcb._extended_extract = _traced_extended

    # ── Patch maybe_browser_refetch to log its 4-tuple return ────────────────
    _orig_browser = _pcb.maybe_browser_refetch

    async def _traced_browser(url, payload, emit=None, force=False):
        result = await _orig_browser(url, payload, emit=emit, force=force)
        filled, evidence, rendered_html, override = result
        print("\n[MASTER_RESEARCH_TRACE] maybe_browser_refetch (override={})".format(override))
        for k in TRACE_FIELDS:
            if k in filled:
                print("[MASTER_RESEARCH_TRACE]   RETURNED {:25s} = {!r}".format(
                    k, filled[k]))
        return result

    _pcb.maybe_browser_refetch = _traced_browser

    # ── Patch enforce_source_evidence to log drops ───────────────────────────
    import app.services.scraper.stage_course as _stg
    _orig_ese = _guards.enforce_source_evidence

    def _traced_ese(payload, evidence):
        cleaned, dropped = _orig_ese(payload, evidence)
        if dropped:
            print("\n[MASTER_RESEARCH_TRACE] enforce_source_evidence DROPPED: {}".format(
                dropped))
        else:
            print("\n[MASTER_RESEARCH_TRACE] enforce_source_evidence: nothing dropped")
        for ev in (evidence or []):
            if ev.get("field_key") in TRACE_FIELDS:
                src = bool((ev.get("source_url") or "").strip())
                snp = bool((ev.get("snippet") or "").strip())
                old_snp = bool((ev.get("source_text") or "").strip())
                print(
                    "[MASTER_RESEARCH_TRACE]   EVIDENCE  {:25s} method={:35s} "
                    "src={} snip={} src_text={}".format(
                        ev.get("field_key", ""),
                        ev.get("method", ""),
                        "Y" if src else "N",
                        "Y" if snp else "N",
                        "Y" if old_snp else "N",
                    )
                )
        return cleaned, dropped

    _guards.enforce_source_evidence = _traced_ese
    _stg.enforce_source_evidence = _traced_ese

    # ── Emit helper ──────────────────────────────────────────────────────────
    _INTERESTING = (
        "[GEMINI]", "[FALLBACK]", "[UOW", "[RENDERED",
        "per-course browser", "[FIELD TRACE]", "[PG-SKIP",
        "[CENTRAL]", "[VIT", "[FEE]",
    )

    async def emit(event_type, msg="", **kwargs):
        if any(tag in str(msg) for tag in _INTERESTING):
            print("  LOG: {}".format(str(msg)[:160]))

    # ── Run extraction ───────────────────────────────────────────────────────
    from app.services.scraper.pipelines.single_course import extract_course

    print("\n[MASTER_RESEARCH_TRACE] Starting extraction:")
    print("[MASTER_RESEARCH_TRACE] {}".format(TARGET_URL))

    result = await extract_course(
        TARGET_URL,
        emit=emit,
        use_ai_fallback=True,
    )

    payload = result.get("payload", {})
    evidence = result.get("evidence", [])

    _snap("AFTER extract_course (before stage_course)", payload)

    # Print all evidence for traced fields
    print("\n[MASTER_RESEARCH_TRACE] All evidence for traced fields:")
    for ev in evidence:
        if ev.get("field_key") in TRACE_FIELDS:
            src = (ev.get("source_url") or "")[:60]
            snp = (ev.get("snippet") or ev.get("source_text") or "")[:80]
            print("[MASTER_RESEARCH_TRACE]   {:25s} method={:30s} src={} snip_key={} val={!r}".format(
                ev.get("field_key", ""),
                ev.get("method", ""),
                "Y" if src else "N",
                "snippet" if ev.get("snippet") else ("source_text" if ev.get("source_text") else "NONE"),
                ev.get("value"),
            ))

    # Simulate enforce_source_evidence
    print("\n[MASTER_RESEARCH_TRACE] Simulating enforce_source_evidence...")
    cleaned, dropped = _orig_ese(payload, evidence)
    _snap("AFTER enforce_source_evidence", cleaned)

    # DB check
    print("\n[MASTER_RESEARCH_TRACE] Checking DB for existing row...")
    try:
        from app.db.session import get_async_session_context
        from sqlalchemy import text as sql_text
        async with get_async_session_context() as db:
            row = (await db.execute(
                sql_text(
                    "SELECT course_name, international_fee, ielts_overall, "
                    "duration, duration_term, intake_months, status "
                    "FROM scraped_courses "
                    "WHERE course_website LIKE '%master-of-research%' "
                    "ORDER BY scraped_at DESC LIMIT 1"
                )
            )).fetchone()
            if row:
                print("[MASTER_RESEARCH_TRACE] DB ROW: {}".format(dict(row._mapping)))
            else:
                print("[MASTER_RESEARCH_TRACE] DB ROW: not found")
    except Exception as e:
        print("[MASTER_RESEARCH_TRACE] DB check error: {}".format(e))

    print("\n[MASTER_RESEARCH_TRACE] Done.\n")


if __name__ == "__main__":
    asyncio.run(run())
