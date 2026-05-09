"""AI fallback extractor.

Runs only for the fields the rule-based extractors left empty. Sends a
trimmed text excerpt to Gemini and parses the JSON response. Respects the
per-day budget and silently no-ops when GEMINI_API_KEY is missing or the
budget is exhausted.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.services.ai import gemini_client
from app.services.scraper.extractors._text import html_to_text

log = logging.getLogger(__name__)

_FIELD_HINTS: dict[str, str] = {
    "international_fee": "Annual international tuition fee in the page's currency. Number only.",
    "fee_currency": "ISO currency code of the international tuition (AUD, USD, GBP, etc.).",
    "ielts_overall": "Required IELTS overall band score (e.g. 6.5). Number only.",
    "intake_months": "List of month names (e.g. [\"January\", \"March\", \"July\"]) when this course starts.",
    "course_location": (
        "Campus city/location where this course is physically taught "
        "(e.g. 'Melbourne', 'Ballarat', 'Sydney, Brisbane'). "
        "Use real place names only. Omit 'Online' or 'Virtual'. "
        "Null if not stated."
    ),
    # Bug: prod ASA Masters rows showed duration=5 because the page text
    # contained "5 units of 8 credit points each across 2 years" and the
    # vague old hint ("Course duration as a number") let Gemini return 5.
    # The regex extractor has _CREDIT_POINT_CONTEXT to defend against this
    # but the AI fallback was unguarded. Be explicit: total elapsed time,
    # ignore credit points / unit counts / per-trimester loads.
    "duration_value": (
        "Total program duration from enrolment to graduation, full-time. "
        "Report ONLY the elapsed time (e.g. 2 for a 2-year Masters, "
        "3 for a 3-year Bachelors). DO NOT report credit-point counts, "
        "unit counts, or per-trimester subject loads. If the page says "
        "'5 units of 8 credit points across 2 years' the answer is 2 (years), "
        "not 5. If the page gives a range (e.g. 'Minimum 2 years, up to a "
        "maximum of 5 years'), return the MINIMUM (shortest) value — i.e. 2, not 5."
    ),
    "duration_unit": (
        'Unit for duration_value. Use "years" for programs measured in years '
        '(typical Bachelors/Masters), "months" for short programs. NEVER use '
        '"units" or "credit points".'
    ),
}

# Week 1 Prompt 5 — non-negotiable rules block prepended to the AI fallback
# prompt.  Tightens priority/refusal/guess-prevention so the model:
#   * never copies Domestic-section values into international_fee
#   * never overrides values another extractor already produced
#   * returns null instead of inferring from typical Australian pricing /
#     band thresholds
#   * never treats page-chrome headings ("Key Information", "Course Rules")
#     as location/intake/category values
# Verification: re-scrape Torrens / UTAS / VIT and check ai_fallback row count
# drops materially — see prompt notes.
_CRITICAL_RULES_BLOCK = """CRITICAL RULES:

1. PRIORITY ORDER for missing fields:
   a. If the page has separate Domestic and International sections,
      extract international_fee, ielts_overall, etc. from the
      International section ONLY.
   b. Use Domestic section only for domestic_fee.
   c. Never put a Domestic section value into international_fee.
   d. Never put a CSP / Commonwealth Supported Place value into international_fee.

2. NEVER OVERRIDE existing extraction:
   - You are filling gaps left by other extractors.
   - If a field already has a value when you receive this request,
     your job is to find OTHER missing fields, not correct that one.

3. NEVER GUESS:
   - If the page text does not explicitly contain a value for a field,
     return null for that field.
   - Do not infer fees from typical Australian university pricing.
   - Do not infer IELTS bands from typical course requirements.
   - Do not extract values from page chrome (headers, navigation, footers).
   - Returning null is correct when the value is not present.

4. SECTION HEADERS are NOT values:
   - "Key Information", "Entry Requirements", "Course Rules"
     are page chrome, not location/intake/category values.

These rules are non-negotiable.
"""


_PROMPT_TEMPLATE = """You are a strict data extractor for a university course page.
""" + _CRITICAL_RULES_BLOCK + """
Return ONLY a JSON object with the requested fields. Use null when the page does
not state the value explicitly. Do NOT invent values.

Fields to extract:
{fields_block}

Course URL: {url}

Course page text (truncated):
\"\"\"
{text}
\"\"\"
"""


def _trim_text(html: str, *, max_chars: int = 25000) -> str:
    text = html_to_text(html)
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2 :]
    return f"{head}\n...\n{tail}"


def _parse_json(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    # Strip ```json ... ``` fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    # Try greedy match first
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            pass
    # Fallback: parse line-by-line "key": value pairs even if JSON is truncated
    out: dict[str, Any] = {}
    for line in raw.splitlines():
        m2 = re.match(r'\s*"([^"]+)"\s*:\s*(.+?),?\s*$', line)
        if not m2:
            continue
        key, val_str = m2.group(1), m2.group(2).rstrip(",").strip()
        if val_str == "null":
            out[key] = None
        elif val_str.startswith('"') and val_str.endswith('"'):
            out[key] = val_str[1:-1]
        elif val_str.startswith("["):
            try:
                out[key] = json.loads(val_str.rstrip(","))
            except Exception:
                pass
        else:
            try:
                out[key] = float(val_str) if "." in val_str else int(val_str)
            except ValueError:
                out[key] = val_str
    return out


def _coerce(field_key: str, value: Any) -> Any | None:
    if value is None or value == "" or value == []:
        return None
    if field_key in {"international_fee", "ielts_overall", "duration_value"}:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if field_key == "intake_months":
        _MONTH_NAMES = [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ]
        _MONTH_ABBR_MAP = {m[:3].lower(): m for m in _MONTH_NAMES}
        if not isinstance(value, list):
            return None
        out: list[str] = []
        for v in value:
            # Accept integer month numbers (1-12) → convert to name
            if isinstance(v, (int, float)):
                n = int(v)
                if 1 <= n <= 12:
                    out.append(_MONTH_NAMES[n - 1])
                continue
            # Accept string: either a full name or an abbreviation
            if isinstance(v, str):
                s = v.strip()
                # Try full/partial name match
                abbr = s[:3].lower()
                if abbr in _MONTH_ABBR_MAP:
                    out.append(_MONTH_ABBR_MAP[abbr])
                else:
                    # Try parsing as an integer string
                    try:
                        n = int(s)
                        if 1 <= n <= 12:
                            out.append(_MONTH_NAMES[n - 1])
                    except ValueError:
                        pass
        return out or None
    if field_key in {"fee_currency", "duration_unit", "course_location"}:
        return str(value).strip() or None
    return value


# Week 1 Prompt 8 — AI-fallback hallucination filter.
#
# Before any AI-fallback value is merged into ``payload``, the caller must
# verify the value (or a close variant) appears verbatim in the rendered
# course-page text.  This catches the residual hallucinations that even a
# tightened prompt (Prompt 5) cannot eliminate — e.g. Gemini quoting the
# "typical Australian Masters fee" of $38,950 when the page only says
# "fees vary by major".
#
# The validator returns ``True`` (= keep the value) for ``None``/empty
# values so the caller's loop short-circuits cheaply.  Caller is expected
# to log ``[AI_FALLBACK REJECT] ...`` when it discards a value.
def validate_ai_fallback_value(field: str, value: Any, page_text: str) -> bool:
    """Return ``True`` iff ``value`` plausibly appears in ``page_text``.

    Field-specific rules:

    * ``international_fee`` / ``domestic_fee`` — accept if the integer
      digits ("38950") OR the comma-grouped form ("38,950") appear.
    * ``ielts_overall`` / ``pte_overall`` / ``toefl_overall`` — accept
      when the score string is present.  IELTS scores like ``6.5`` and
      ``7`` are normalised to handle trailing-zero variants.
    * Categorical fields (``intake_text``, ``category``, ``sub_category``,
      ``mode``, ``location_text``, ``course_location``, ``intake_months``)
      — accept when every comma-separated token appears in the text
      (case-insensitive, whitespace-trimmed).
    * Any other field — pass-through (returns ``True``); the validator
      only knows how to police the fields the AI fallback actually fills.
    """
    if value is None or value == "" or value == []:
        return True

    page_text_norm = (page_text or "").lower()
    if not page_text_norm:
        # No page text to validate against — be conservative and accept
        # so we don't drop everything when extractor returned empty html.
        return True

    if field in ("international_fee", "domestic_fee"):
        try:
            n = int(float(value))
        except (TypeError, ValueError):
            return False
        if n <= 0:
            return False
        digits = str(n)
        digits_with_comma = f"{n:,}"
        return digits in page_text_norm or digits_with_comma.lower() in page_text_norm

    if field in ("ielts_overall", "pte_overall", "toefl_overall",
                 "cambridge_overall", "duolingo_overall"):
        try:
            score = float(value)
        except (TypeError, ValueError):
            return False
        # Both "6.5" and "6" trailing-zero variants must be considered.
        candidates = {str(value), str(score)}
        if score == int(score):
            candidates.add(str(int(score)))
            candidates.add(f"{int(score)}.0")
        return any(c.lower() in page_text_norm for c in candidates)

    if field == "intake_months":
        # ``intake_months`` is a list of month names — every name must
        # appear at least once on the page (case-insensitive).
        if not isinstance(value, list):
            return True
        return all(
            isinstance(m, str) and m.strip().lower() in page_text_norm
            for m in value
        )

    if field in ("intake_text", "category", "sub_category", "mode",
                 "location_text", "course_location"):
        tokens = [t.strip().lower() for t in str(value).split(",")]
        return all(token in page_text_norm for token in tokens if token)

    return True


async def fill_missing(
    payload: dict[str, Any],
    *,
    html: str,
    url: str,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Return ``{field_key: value}`` for fields the rule-based pass missed.

    Mutates nothing; the caller decides whether to merge.
    """
    candidates = list(fields) if fields else list(_FIELD_HINTS.keys())
    missing = [f for f in candidates if not payload.get(f) and f in _FIELD_HINTS]
    if not missing:
        return {}

    fields_block = "\n".join(f"- {f}: {_FIELD_HINTS[f]}" for f in missing)
    prompt = _PROMPT_TEMPLATE.format(
        fields_block=fields_block, url=url, text=_trim_text(html)
    )
    resp = await gemini_client.generate(prompt, max_output_tokens=2048)
    if resp.skipped:
        log.info("AI fallback skipped for %s: %s", url, resp.skip_reason)
        return {}

    data = _parse_json(resp.text)
    out: dict[str, Any] = {}
    for f in missing:
        coerced = _coerce(f, data.get(f))
        if coerced is not None:
            out[f] = coerced
    if out:
        log.info("AI fallback filled %s for %s (cost=$%.6f)", list(out), url, resp.cost_usd)
    return out
