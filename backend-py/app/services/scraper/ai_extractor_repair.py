"""AI Extraction Rule Repair — Phase 2 of the autonomous pipeline.

When the CASCADE self-heal logic determines that *discovery worked* (enough
courses staged) but *extraction failed* (average completeness < 70 %), this
module:

1. Computes per-field fill rates from ``scraped_field_evidence`` for the run.
2. Identifies fields whose fill rate is below a threshold (default 50 %).
3. Re-fetches 3 sample course pages from the same run.
4. Asks Gemini to generate corrected rules for the failing fields, given the
   old rules that didn't work + fresh HTML.
5. Merges the new rules into ``auto_config["extraction_rules"]`` and persists.
6. Queues a new scrape job so the fixed rules are used immediately.

This is the extraction-layer counterpart of ``probe_and_configure`` (which
repairs the *discovery* layer when discovery fails).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# Fields that are critical — we specifically care about their fill rates
CRITICAL_FIELDS = [
    "course_name",
    "degree_level",
    "study_mode",
    "duration",
    "intake_months",
    "international_fee",
    "english_test",
    "other_requirement",
]

# ── Fill-rate computation ──────────────────────────────────────────────────────

async def compute_field_fill_rates(
    scrape_run_id: int,
    db: Any,
) -> dict[str, float]:
    """Compute per-field fill rates for a scrape run.

    Queries ``scraped_field_evidence`` for all *selected* rows belonging to
    the run, then groups by ``field_key`` to compute:
        fill_rate = courses_with_value / total_staged_courses

    Returns a dict ``{field_key: fill_rate_0_to_1}``.
    An empty dict means the run had no evidence rows (very early failure).
    """
    from sqlalchemy import text as _t
    try:
        # Total staged courses for this run
        total_row = await db.execute(_t(
            "SELECT COUNT(*) FROM scraped_courses WHERE scrape_job_id = :run_id"
        ), {"run_id": scrape_run_id})
        total: int = total_row.scalar() or 0
        if total == 0:
            return {}

        # Per-field selected evidence rows (one per course per field)
        rows = await db.execute(_t("""
            SELECT sfe.field_key, COUNT(*) AS filled_count
            FROM scraped_field_evidence sfe
            JOIN scraped_courses sc ON sc.id = sfe.scraped_course_id
            WHERE sc.scrape_job_id = :run_id
              AND sfe.selected = TRUE
              AND sfe.candidate_value IS NOT NULL
              AND sfe.candidate_value != ''
            GROUP BY sfe.field_key
        """), {"run_id": scrape_run_id})
        fill_rates: dict[str, float] = {}
        for row in rows:
            field_key, filled_count = row
            fill_rates[field_key] = round(filled_count / total, 3)

        log.info(
            "[EXTRACTOR_REPAIR] fill rates for run %d (total=%d): %s",
            scrape_run_id, total,
            {k: f"{v:.0%}" for k, v in sorted(fill_rates.items())},
        )
        return fill_rates

    except Exception as exc:
        log.error("[EXTRACTOR_REPAIR] fill rate query failed: %s", exc)
        return {}


def identify_failing_fields(
    fill_rates: dict[str, float],
    threshold: float = 0.50,
) -> list[str]:
    """Return fields whose fill rate is below *threshold*.

    Prioritises :data:`CRITICAL_FIELDS` — if any critical field is missing
    from ``fill_rates`` entirely (never extracted) it is included automatically.
    """
    failing: list[str] = []

    # Fields with a measured fill rate below threshold
    for field, rate in fill_rates.items():
        if rate < threshold:
            failing.append(field)

    # Critical fields that never appeared at all
    for field in CRITICAL_FIELDS:
        if field not in fill_rates and field not in failing:
            failing.append(field)

    return sorted(set(failing))


# ── Sample page re-fetching ───────────────────────────────────────────────────

async def fetch_repair_samples(
    scrape_run_id: int,
    db: Any,
    n: int = 3,
) -> list[tuple[str, str]]:
    """Fetch HTML for up to *n* course URLs from the run.

    Returns a list of ``(url, html)`` tuples (empty on failure).
    URLs are picked from ``scraped_courses.source_url`` for the run.
    """
    from sqlalchemy import text as _t
    try:
        rows = await db.execute(_t("""
            SELECT DISTINCT source_url
            FROM scraped_courses
            WHERE scrape_job_id = :run_id
              AND source_url IS NOT NULL
            ORDER BY id
            LIMIT :n
        """), {"run_id": scrape_run_id, "n": n})
        urls = [r[0] for r in rows if r[0]]
    except Exception as exc:
        log.error("[EXTRACTOR_REPAIR] sample URL query failed: %s", exc)
        return []

    from app.services.scraper.auto_config_generator import fetch_sample_course_html
    samples: list[tuple[str, str]] = []
    for url in urls:
        try:
            html = await fetch_sample_course_html(url, timeout=12.0)
            if html:
                samples.append((url, html))
        except Exception as exc:
            log.debug("[EXTRACTOR_REPAIR] fetch failed for %s: %s", url, exc)
    return samples


# ── Rule regeneration ─────────────────────────────────────────────────────────

_REPAIR_SYSTEM = """\
You are an expert web scraping engineer.
A previous attempt to generate CSS/XPath/regex extraction rules for this university website
produced poor results — the listed fields are failing (fill rate below 50 %).

Your task: generate IMPROVED extraction rules for ONLY the failing fields.
Study the HTML carefully and find where each piece of data actually lives.
"""

_REPAIR_USER = """\
UNIVERSITY URL: {url}

FAILING FIELDS (fill rate < 50%): {failing}

PREVIOUS RULES THAT DID NOT WORK:
{old_rules}

SAMPLE PAGE HTML (up to 5000 chars each — {n_samples} sample(s)):
{html_blocks}

OUTPUT: A JSON object with one key per failing field. Each value is a rule object:
{{
  "field_name": {{
    "css": "...",      // preferred
    "xpath": "...",    // fallback
    "regex": "...",    // last resort — one capture group
    "attribute": "...",// optional — HTML attribute instead of inner text
    "transform": "...",// optional: "currency","number","list","month_list"
    "quoted_text": "...",// REQUIRED: verbatim text from the HTML that this rule matches
    "confidence": 0.90
  }}
}}

RULES:
- Only output rules for the FAILING fields.
- quoted_text MUST appear verbatim in the sample HTML above.
- If you cannot find a reliable extraction point, set confidence < 0.5.
- Respond ONLY with valid JSON — no markdown, no explanation.
"""


async def repair_extraction_rules(
    failing_fields: list[str],
    current_rules: dict[str, Any],
    samples: list[tuple[str, str]],
    uni_url: str,
) -> dict[str, Any]:
    """Ask Gemini to generate corrected rules for failing fields.

    Returns a dict of repaired rules (may be empty if Gemini is unavailable).
    The caller should merge these into ``auto_config["extraction_rules"]``
    (new rules override old ones field-by-field).
    """
    if not failing_fields or not samples:
        log.info("[EXTRACTOR_REPAIR] Nothing to repair (no failing fields or no samples)")
        return {}

    from app.services.scraper.ai_extractor_gen import _clean_html_for_prompt, _validate_rule

    # Build HTML blocks (one per sample, 5000 chars each)
    html_blocks = ""
    for i, (url, html) in enumerate(samples[:3], 1):
        excerpt = _clean_html_for_prompt(html, max_chars=5000 // len(samples))
        html_blocks += f"\n--- Sample {i}: {url} ---\n{excerpt}\n"

    old_rules_json = json.dumps(
        {f: current_rules.get(f, {}) for f in failing_fields},
        indent=2,
    )

    prompt = _REPAIR_SYSTEM + "\n\n" + _REPAIR_USER.format(
        url=uni_url,
        failing=json.dumps(failing_fields),
        old_rules=old_rules_json,
        n_samples=len(samples),
        html_blocks=html_blocks,
    )

    from app.services.ai import gemini_client  # type: ignore[attr-defined]
    try:
        resp = await gemini_client.generate(prompt=prompt, call_type="extractor_repair")
    except Exception as exc:
        log.warning("[EXTRACTOR_REPAIR] Gemini call failed: %s", exc)
        return {}

    if resp.skipped or not resp.text:
        log.info("[EXTRACTOR_REPAIR] Gemini skipped — no repaired rules")
        return {}

    raw = resp.text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.M).strip()
    try:
        parsed: dict = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("[EXTRACTOR_REPAIR] Non-JSON response: %s — %r", exc, raw[:200])
        return {}

    if not isinstance(parsed, dict):
        return {}

    # Use the full combined HTML for validation
    combined_html = "\n".join(h for _, h in samples)

    repaired: dict[str, Any] = {}
    for field, rule in parsed.items():
        if not isinstance(rule, dict):
            continue
        if float(rule.get("confidence", 0.0)) < 0.5:
            continue
        if not _validate_rule(field, rule, combined_html):
            log.debug("[EXTRACTOR_REPAIR] %s: repaired rule failed validation", field)
            continue
        repaired[field] = rule

    log.info(
        "[EXTRACTOR_REPAIR] Repaired %d/%d failing fields: %s",
        len(repaired), len(failing_fields), sorted(repaired.keys()),
    )
    return repaired


# ── Persistence helper ────────────────────────────────────────────────────────

async def apply_repaired_rules_to_db(
    university_id: int,
    repaired_rules: dict[str, Any],
    db: Any,
) -> bool:
    """Merge repaired rules into ``universities.scrape_config["auto_config"]``.

    Returns True on success.
    """
    from sqlalchemy import text as _t
    try:
        row = await db.execute(
            _t("SELECT scrape_config FROM universities WHERE id = :id"),
            {"id": university_id},
        )
        sc_row = row.first()
        if not sc_row:
            log.error("[EXTRACTOR_REPAIR] university %d not found", university_id)
            return False

        scrape_config: dict = dict(sc_row[0] or {})
        ac: dict = scrape_config.setdefault("auto_config", {})
        existing_rules: dict = ac.setdefault("extraction_rules", {})
        existing_rules.update(repaired_rules)
        ac["extraction_rules"] = existing_rules
        ac["_extraction_rules_repaired_at"] = (
            __import__("datetime").datetime.utcnow().isoformat()
        )
        scrape_config["auto_config"] = ac

        await db.execute(
            _t("UPDATE universities SET scrape_config = :sc WHERE id = :id"),
            {"sc": json.dumps(scrape_config), "id": university_id},
        )
        await db.commit()
        log.info(
            "[EXTRACTOR_REPAIR] Persisted %d repaired rules for uni_id=%d",
            len(repaired_rules), university_id,
        )
        return True

    except Exception as exc:
        log.error("[EXTRACTOR_REPAIR] DB update failed: %s", exc)
        try:
            await db.rollback()
        except Exception:
            pass
        return False
