"""Conservative English-score rescue from embedded script/JSON content."""
from __future__ import annotations

import html as html_lib
import re
from typing import Any

from app.services.scraper.extractors.english_test import _ielts

_SCRIPT_RE = re.compile(
    r"<script\b[^>]*>(?P<body>.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)
_IELTS_TOKEN_RE = re.compile(r"\bIELTS\b", re.IGNORECASE)
_FIELD_MAP = {
    "overall": "ielts_overall",
    "listening": "ielts_listening",
    "reading": "ielts_reading",
    "writing": "ielts_writing",
    "speaking": "ielts_speaking",
}


def _plain_script_text(raw: str) -> str:
    """Decode common JSON/HTML escaping without executing script content."""
    text = html_lib.unescape(raw or "")
    text = (
        text.replace(r"\u003c", "<")
        .replace(r"\u003e", ">")
        .replace(r"\u0026", "&")
        .replace(r"\/", "/")
        .replace(r"\"", '"')
        .replace(r"\n", " ")
        .replace(r"\r", " ")
        .replace(r"\t", " ")
    )
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_unambiguous_ielts(
    html: str,
) -> tuple[dict[str, float], str | None]:
    """Return one explicit IELTS profile from scripts, or fail closed.

    Pages can embed aggregate/search-card data beside the current course.
    When script content contains conflicting IELTS profiles, returning no
    result is safer than assigning another course's requirement.
    """
    profiles: dict[tuple[tuple[str, float], ...], tuple[dict[str, float], str]] = {}
    for script_match in _SCRIPT_RE.finditer(html or ""):
        body = script_match.group("body")
        if "ielts" not in body.lower():
            continue
        text = _plain_script_text(body)
        for token_match in _IELTS_TOKEN_RE.finditer(text):
            snippet = text[
                max(0, token_match.start() - 120) :
                min(len(text), token_match.end() + 360)
            ].strip()
            parsed = _ielts(snippet)
            if not parsed or "overall" not in parsed:
                continue
            normalized = {
                key: float(value)
                for key, value in parsed.items()
                if key in _FIELD_MAP and value is not None
            }
            signature = tuple(sorted(normalized.items()))
            profiles.setdefault(signature, (normalized, snippet))
    if len(profiles) != 1:
        return {}, None
    return next(iter(profiles.values()))


def apply_embedded_english(
    payload: dict[str, Any],
    html: str,
    *,
    url: str,
    evidence: list[dict[str, Any]],
) -> list[str]:
    """Fill missing IELTS slots from one proven embedded-data profile."""
    parsed, snippet = extract_unambiguous_ielts(html)
    if not parsed or not snippet:
        return []

    existing_overall = payload.get("ielts_overall")
    parsed_overall = parsed.get("overall")
    if (
        existing_overall not in (None, "", 0)
        and parsed_overall is not None
        and abs(float(existing_overall) - parsed_overall) > 0.05
    ):
        return []

    filled: list[str] = []
    for source_key, field_key in _FIELD_MAP.items():
        value = parsed.get(source_key)
        if value is None or payload.get(field_key) not in (None, "", 0):
            continue
        payload[field_key] = value
        evidence.append(
            {
                "field_key": field_key,
                "value": value,
                "confidence": 0.9,
                "method": "embedded_json:english",
                "source_url": url,
                "snippet": snippet,
            }
        )
        filled.append(field_key)
    return filled