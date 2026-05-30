"""AI Extraction Rule Generator — Phase 2 of the autonomous pipeline.

Given sample HTML from a real course page, this module asks Gemini to produce
per-field extraction rules (CSS selector, XPath, or regex).  The rules are
stored in ``auto_config["extraction_rules"]`` and applied by
:mod:`ai_extractor_run` as **Stage 0** inside ``extract_course()`` — before
any regex heuristics and before any per-course Gemini call.

This is how Gemini cost-per-course drops to zero for well-configured sites:
Gemini runs once at *probe time* to understand the site structure, and the
generated rules do all the work on every subsequent course page.

Rule format (per field)
-----------------------
::

    {
        "css": ".fee-amount",          # BeautifulSoup CSS selector (preferred)
        "xpath": "//td[@class='fee']", # XPath fallback
        "regex": r"\\$([\\d,]+)",       # regex with one capture group
        "attribute": "data-value",     # HTML attribute to read (vs inner text)
        "transform": "currency",       # optional post-processing hint
        "quoted_text": "AUD 45,000",   # text the rule must find in the HTML
        "confidence": 0.90,            # 0.0–1.0 self-reported confidence
    }

Validation
----------
Each rule must include ``quoted_text`` — a verbatim string that actually
appears in the sample HTML and that the rule would extract.  Rules whose
``quoted_text`` is absent from the HTML are rejected (hallucination guard).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# ── Canonical field list (matches completeness.py review fields) ──────────────
EXTRACTION_FIELDS = [
    "course_name",
    "degree_level",
    "category",
    "study_mode",
    "course_location",
    "duration",
    "intake_months",
    "international_fee",
    "description",
    "academic_level",
    "academic_score",
    "english_test",          # e.g. "IELTS 6.5"
    "other_requirement",
]

# Additional non-review fields that are useful to extract when possible
BONUS_FIELDS = [
    "ielts_overall",
    "pte_overall",
    "toefl_overall",
    "domestic_fee",
    "fee_currency",
    "fee_term",
    "duration_term",
    "cricos_code",
]

_SYSTEM_PROMPT = """\
You are an expert web scraping engineer specialising in university course catalogue pages.
Your task: analyse a real course page and produce precise extraction rules for each field.

RULE TYPES (in preference order):
1. css       — BeautifulSoup CSS selector string (e.g. ".tuition-fee .amount")
2. xpath     — XPath 1.0 expression (e.g. "//td[contains(@class,'fee')]")
3. regex     — Python regex with ONE capture group applied to the full page text
4. attribute — HTML attribute name to read from the matched element (default: inner text)
5. transform — optional post-processing hint: "currency", "number", "list", "month_list"

REQUIREMENTS:
- Prefer css over xpath; prefer xpath over regex.
- Provide at most ONE rule type per field (the best you found).
- quoted_text MUST be a verbatim substring from the HTML I gave you that your rule would match.
  This is used as a sanity check. If you cannot find a clear match, set confidence < 0.5.
- If a field is not present on this page type, set confidence: 0.0 and omit css/xpath/regex.
- Return ONLY valid JSON — no markdown, no explanation.
"""

_USER_TEMPLATE = """\
TARGET FIELDS: {fields}

PAGE URL: {url}

PAGE HTML (up to 6000 chars):
{html_excerpt}

TASK: For each target field, output a rule JSON object.

OUTPUT FORMAT (fill in values, omit fields with no match):
{{
  "course_name":      {{"css": "...", "quoted_text": "...", "confidence": 0.95}},
  "international_fee":{{"css": "...", "attribute": "...", "transform": "currency", "quoted_text": "...", "confidence": 0.90}},
  "ielts_overall":    {{"regex": "IELTS[\\\\s:]+([\\\\d.]+)", "quoted_text": "...", "confidence": 0.85}},
  ...
}}

Rules:
- Only include fields where you found a real match.
- quoted_text must appear verbatim in the HTML excerpt above.
- confidence: 0.9+ = very confident; 0.7–0.89 = likely correct; <0.7 = uncertain.
- For intake_months use transform "month_list" and regex capturing month names.
- For study_mode capture "Full-time", "Part-time", or "Online".
- For duration use transform "number" and capture e.g. "2" from "2 years".
- Respond ONLY with valid JSON.
"""


def _clean_html_for_prompt(html: str, max_chars: int = 6000) -> str:
    """Strip scripts/styles/comments and truncate for prompt."""
    text = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    # Remove tags but keep structure hints by preserving newlines around block elements
    text = re.sub(r"</(div|section|article|header|footer|main|p|li|td|th|h[1-6])>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_chars]


def _validate_rule(field: str, rule: dict, html: str) -> bool:
    """Return True if the rule's quoted_text appears verbatim in the HTML."""
    qt = (rule.get("quoted_text") or "").strip()
    if not qt:
        log.debug("[EXTRACTOR_GEN] %s: rule has no quoted_text — rejected", field)
        return False
    if qt not in html:
        log.debug(
            "[EXTRACTOR_GEN] %s: quoted_text %r not found in HTML — rejected",
            field, qt[:60],
        )
        return False
    # Must have at least one actionable rule
    if not any(rule.get(k) for k in ("css", "xpath", "regex")):
        log.debug("[EXTRACTOR_GEN] %s: no css/xpath/regex key — rejected", field)
        return False
    return True


async def generate_extraction_rules(
    html: str,
    url: str,
    fields: list[str] | None = None,
    min_confidence: float = 0.5,
) -> dict[str, dict[str, Any]]:
    """Ask Gemini to generate per-field extraction rules from sample HTML.

    Parameters
    ----------
    html:
        Raw HTML of a real course detail page.
    url:
        URL of the page (used in the prompt for context).
    fields:
        List of field names to generate rules for.  Defaults to
        :data:`EXTRACTION_FIELDS` + :data:`BONUS_FIELDS`.
    min_confidence:
        Rules with confidence below this value are dropped.

    Returns
    -------
    dict mapping field name → rule dict.  Empty if Gemini is unavailable or
    the response cannot be parsed.
    """
    if not html:
        log.info("[EXTRACTOR_GEN] No HTML provided — skipping rule generation")
        return {}

    target_fields = fields or (EXTRACTION_FIELDS + BONUS_FIELDS)
    html_excerpt = _clean_html_for_prompt(html)

    from app.services.ai import gemini_client  # type: ignore[attr-defined]

    prompt = _SYSTEM_PROMPT + "\n\n" + _USER_TEMPLATE.format(
        fields=json.dumps(target_fields),
        url=url,
        html_excerpt=html_excerpt,
    )

    try:
        resp = await gemini_client.generate(
            prompt=prompt,
            call_type="extractor_gen",
        )
    except Exception as exc:
        log.warning("[EXTRACTOR_GEN] Gemini call failed: %s", exc)
        return {}

    if resp.skipped or not resp.text:
        log.info("[EXTRACTOR_GEN] Gemini skipped (budget/quota) — no rules generated")
        return {}

    # ── Parse JSON response ──────────────────────────────────────────────────
    raw = resp.text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.M).strip()
    try:
        parsed: dict = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("[EXTRACTOR_GEN] Non-JSON response: %s — %r", exc, raw[:200])
        return {}

    if not isinstance(parsed, dict):
        log.warning("[EXTRACTOR_GEN] Expected dict, got %s", type(parsed).__name__)
        return {}

    # ── Validate and filter rules ────────────────────────────────────────────
    rules: dict[str, dict[str, Any]] = {}
    for field, rule in parsed.items():
        if not isinstance(rule, dict):
            continue
        conf = float(rule.get("confidence", 0.0))
        if conf < min_confidence:
            log.debug("[EXTRACTOR_GEN] %s: confidence %.2f < %.2f — skipped", field, conf, min_confidence)
            continue
        if not _validate_rule(field, rule, html):
            continue
        rules[field] = {k: v for k, v in rule.items() if v is not None and v != ""}

    log.info(
        "[EXTRACTOR_GEN] Generated %d/%d valid rules from %s",
        len(rules), len(target_fields), url,
    )
    return rules


def _build_seeded_prompt(learned_patterns: dict[str, Any]) -> str:
    """Return a prompt section listing proven patterns for this platform.

    Injected between the system prompt and the user template so Gemini
    starts from experience rather than from zero.  Rules are listed as
    *suggestions* — Gemini must confirm each rule against the actual HTML.
    """
    if not learned_patterns:
        return ""
    lines = [
        "\nPROVEN PATTERNS FROM SUCCESSFUL PAST SCRAPES ON THIS PLATFORM:",
        "Use these as a starting point. Adopt them only if the HTML confirms they apply.",
        "If a selector doesn't match the current page, generate a new one instead.",
        json.dumps(
            {
                fk: {k: v for k, v in rule.items() if k in ("css", "xpath", "regex", "attribute", "transform")}
                for fk, rule in learned_patterns.items()
            },
            indent=2,
        ),
        "",
    ]
    return "\n".join(lines)


async def generate_and_store_rules(
    profile: Any,
    sample_html: str,
    auto_config: dict[str, Any],
    learned_patterns: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate extraction rules and merge them into ``auto_config``.

    Called from :func:`auto_config_generator.generate_config` after the
    main Gemini config-generation call.  Adds ``extraction_rules`` key to
    ``auto_config`` in-place and returns the updated dict.

    Parameters
    ----------
    profile:
        :class:`SiteProfile` instance from the probe stage.
    sample_html:
        HTML of one real course page fetched during probe.
    auto_config:
        The auto_config dict being built (modified in-place).
    learned_patterns:
        Phase 3 — rules from ``scraper_patterns`` for this platform type.
        Injected into the Gemini prompt as proven examples; improves
        first-probe quality and cuts token usage for well-known platforms.
    """
    if not sample_html:
        log.info("[EXTRACTOR_GEN] No sample HTML — skipping rule generation")
        return auto_config

    platform_type = auto_config.get("_platform_type", "")
    n_learned = len(learned_patterns) if learned_patterns else 0
    if n_learned:
        log.info(
            "[EXTRACTOR_GEN] Seeding Gemini prompt with %d learned patterns "
            "for platform=%r", n_learned, platform_type,
        )

    # Build the seeding section (empty string if no learned patterns)
    seed_section = _build_seeded_prompt(learned_patterns or {})

    html_excerpt = _clean_html_for_prompt(sample_html)
    target_fields = EXTRACTION_FIELDS + BONUS_FIELDS

    from app.services.ai import gemini_client  # type: ignore[attr-defined]

    prompt = (
        _SYSTEM_PROMPT
        + seed_section
        + "\n\n"
        + _USER_TEMPLATE.format(
            fields=json.dumps(target_fields),
            url=getattr(profile, "url", ""),
            html_excerpt=html_excerpt,
        )
    )

    try:
        resp = await gemini_client.generate(
            prompt=prompt,
            call_type="extractor_gen",
        )
    except Exception as exc:
        log.warning("[EXTRACTOR_GEN] Gemini call failed: %s", exc)
        return auto_config

    if resp.skipped or not resp.text:
        log.info("[EXTRACTOR_GEN] Gemini skipped (budget/quota) — no rules generated")
        # Fall back to learned patterns as-is when Gemini is unavailable
        if learned_patterns:
            auto_config["extraction_rules"] = dict(learned_patterns)
            auto_config["_extraction_rules_source"] = "learned_fallback"
            log.info(
                "[EXTRACTOR_GEN] Fell back to %d learned patterns for platform=%r",
                n_learned, platform_type,
            )
        return auto_config

    # ── Parse JSON response ──────────────────────────────────────────────────
    raw = resp.text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.M).strip()
    try:
        parsed: dict = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("[EXTRACTOR_GEN] Non-JSON response: %s — %r", exc, raw[:200])
        return auto_config

    if not isinstance(parsed, dict):
        log.warning("[EXTRACTOR_GEN] Expected dict, got %s", type(parsed).__name__)
        return auto_config

    # ── Validate and filter rules ────────────────────────────────────────────
    rules: dict[str, dict[str, Any]] = {}
    for field, rule in parsed.items():
        if not isinstance(rule, dict):
            continue
        conf = float(rule.get("confidence", 0.0))
        if conf < 0.5:
            log.debug("[EXTRACTOR_GEN] %s: confidence %.2f < 0.5 — skipped", field, conf)
            continue
        if not _validate_rule(field, rule, sample_html):
            continue
        rules[field] = {k: v for k, v in rule.items() if v is not None and v != ""}

    # Merge: learned patterns fill in any fields Gemini didn't cover
    if learned_patterns:
        for fk, lr in learned_patterns.items():
            if fk not in rules:
                rules[fk] = lr

    log.info(
        "[EXTRACTOR_GEN] Generated %d/%d valid rules from %s "
        "(%d from Gemini, %d backfilled from learned)",
        len(rules), len(target_fields), getattr(profile, "url", "?"),
        len([f for f in rules if f not in (learned_patterns or {})]),
        len([f for f in rules if f in (learned_patterns or {})]),
    )

    if rules:
        auto_config["extraction_rules"] = rules
        auto_config["_extraction_rules_generated_at"] = (
            __import__("datetime").datetime.utcnow().isoformat()
        )
        if n_learned:
            auto_config["_extraction_rules_learned_count"] = n_learned
    else:
        log.info("[EXTRACTOR_GEN] No valid rules generated — auto_config unchanged")

    return auto_config
