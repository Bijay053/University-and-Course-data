"""Small text-normalisation helpers used across extractors."""
from __future__ import annotations

import re
from datetime import datetime

_WS = re.compile(r"\s+")
_MONTH_NAMES = {
    "jan": "January",
    "feb": "February",
    "mar": "March",
    "apr": "April",
    "may": "May",
    "jun": "June",
    "jul": "July",
    "aug": "August",
    "sep": "September",
    "sept": "September",
    "oct": "October",
    "nov": "November",
    "dec": "December",
}


def collapse_ws(s: str | None) -> str:
    if not s:
        return ""
    return _WS.sub(" ", s).strip()


def normalize_month(raw: str | None) -> str | None:
    if not raw:
        return None
    key = raw.strip().lower()[:4].rstrip(".")
    return _MONTH_NAMES.get(key) or _MONTH_NAMES.get(key[:3])


def parse_int(raw: str | None) -> int | None:
    if not raw:
        return None
    digits = re.sub(r"[^0-9]", "", raw)
    return int(digits) if digits else None


def parse_float(raw: str | None) -> float | None:
    if not raw:
        return None
    cleaned = re.sub(r"[^0-9.]", "", raw)
    if not cleaned or cleaned == ".":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"
