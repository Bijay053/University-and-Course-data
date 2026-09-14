"""Authoritative extraction from UNSW degree-page structured fields."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse


def is_unsw_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "unsw.edu.au" or host.endswith(".unsw.edu.au")


def _score(text: str, label: str) -> float | None:
    match = re.search(rf"(\d+(?:\.\d+)?)\s+in\s+{label}\b", text, re.I)
    return float(match.group(1)) if match else None


def _english_fields(html: str) -> dict[str, float]:
    match = re.search(
        r'window\.engRequirementsConfig\s*=\s*"((?:\\.|[^"\\])*)"',
        html,
        re.S,
    )
    if not match:
        return {}
    try:
        decoded = bytes(match.group(1), "utf-8").decode("unicode_escape")
        config = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}

    result: dict[str, float] = {}
    tests = (
        ("ielts", "ielts"),
        ("pearsonsTestOfEnglish", "pte"),
        ("toeflIbt", "toefl"),
        ("c1AdvancedCambridge", "cambridge"),
    )
    for source_key, field_prefix in tests:
        text = str(config.get(source_key) or "")
        overall = re.match(r"\s*(\d+(?:\.\d+)?)\s+Overall\b", text, re.I)
        if not overall:
            continue
        result[f"{field_prefix}_overall"] = float(overall.group(1))
        if field_prefix in {"ielts", "toefl"}:
            for component in ("listening", "reading", "writing", "speaking"):
                value = _score(text, component)
                if value is not None:
                    result[f"{field_prefix}_{component}"] = value
    return result


def apply_unsw_static_extraction(url: str, html: str) -> dict[str, Any]:
    if not is_unsw_url(url):
        return {}

    result: dict[str, Any] = {}
    fee = re.search(
        r"cmp-contentfragment__element--internationalAnnual\b.*?"
        r"</dt>\s*<dd\b[^>]*>\s*\$([\d,]+)",
        html,
        re.I | re.S,
    )
    if fee:
        result.update(
            {
                "international_fee": float(fee.group(1).replace(",", "")),
                "fee_currency": "AUD",
                "fee_term": "Annual",
            }
        )
    result.update(_english_fields(html))
    return result