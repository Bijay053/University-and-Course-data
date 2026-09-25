"""Source-bound reviewer fee choices. Never discard the extracted alternatives."""
from __future__ import annotations

import hashlib
import json
import math

from app.services.scraper.extractors.ulaw_fees import METHOD, is_ulaw_course

FIELDS = ("international_fee", "currency", "fee_year", "fee_term")


def _get(row, key):
    return row.get(key) if isinstance(row, dict) else getattr(row, key, None)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, allow_nan=False).encode()).hexdigest()


def source_options(row):
    metadata = _get(row, "extraction_method")
    url = _get(row, "course_website")
    if not isinstance(url, str) or not is_ulaw_course(url) or not isinstance(metadata, dict):
        return None
    if metadata.get("international_fee") not in {METHOD, METHOD + ":null"}:
        return None
    variants = metadata.get("fee_variants")
    if not isinstance(variants, dict) or variants.get("status") not in {"range", "uniform"}:
        return None
    options = variants.get("options")
    if not isinstance(options, list) or not options:
        return None
    for option in options:
        if not isinstance(option, dict):
            return None
        amount, year = option.get("amount"), option.get("year")
        if (isinstance(amount, bool) or not isinstance(amount, (float, int))
                or not math.isfinite(amount) or amount <= 0
                or type(year) is not int or not 2000 <= year <= 2200
                or option.get("currency") != "GBP"
                or option.get("period") not in {"Annual", "Full Course"}
                or not isinstance(option.get("source_url"), str)
                or option["source_url"].rstrip("/") != url.rstrip("/")
                or not isinstance(option.get("campus"), str) or not option["campus"].strip()
                or not isinstance(option.get("study_variant"), str) or not option["study_variant"].strip()
                or not isinstance(option.get("snippet"), str)
                or not option["snippet"].startswith("International Students | ")):
            return None
    selected = variants.get("selected")
    if not isinstance(selected, list) or not selected or any(o not in options for o in selected):
        return None
    # Historical cohorts were deliberately excluded by extraction. Later
    # published years and alternative awards/periods remain explicit choices.
    floor = min(o["year"] for o in selected)
    return [o for o in options if o["year"] >= floor]


def fee_selection(row):
    options = source_options(row)
    if not options:
        return None
    metadata = _get(row, "extraction_method")
    source = digest({"url": _get(row, "course_website"), "variants": metadata["fee_variants"]})
    choice = metadata.get("fee_selection") or {}
    if not isinstance(choice, dict):
        return None
    selected = None
    for option in options:
        if (choice.get("sourceFingerprint") == source and choice.get("optionId") == digest(option)
                and tuple(_get(row, k) for k in FIELDS)
                == (option["amount"], option["currency"], option["year"], option["period"])):
            selected = digest(option)
    try:
        token = digest({
            "id": _get(row, "id"), "job": _get(row, "scrape_job_id"),
            "status": _get(row, "status"), "source": source,
            "tuple": [_get(row, k) for k in FIELDS], "choice": choice,
        })
    except (TypeError, ValueError):
        return None
    return {
        "snapshotToken": token,
        "options": [{"optionId": digest(o), **{
            {"study_variant": "studyVariant", "source_url": "sourceUrl"}.get(k, k): v
            for k, v in o.items()
        }} for o in options],
        "selectedOptionId": selected,
    }


def unresolved_fee_selection(row):
    metadata = _get(row, "extraction_method") or {}
    variants = metadata.get("fee_variants") if isinstance(metadata, dict) else None
    if not isinstance(variants, dict):
        return False
    state = fee_selection(row)
    # A manual scalar edit cannot resolve ambiguous source alternatives.
    return (variants.get("status") == "range" or "fee_selection" in metadata) and not (
        state and state["selectedOptionId"]
    )