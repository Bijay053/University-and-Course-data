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
    "intake_months": "List of intake month numbers (1-12) when this course starts.",
    "duration_value": "Course duration as a number.",
    "duration_unit": 'Duration unit ("years" or "months").',
}

_PROMPT_TEMPLATE = """You are a strict data extractor for a university course page.
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
        if not isinstance(value, list):
            return None
        out = []
        for v in value:
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if 1 <= n <= 12:
                out.append(n)
        return out or None
    if field_key in {"fee_currency", "duration_unit"}:
        return str(value).strip() or None
    return value


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
